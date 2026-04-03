"""lib/swarm/master.py — Master orchestrator: spawn, track, collect sub-agents.

The master agent:
  1. Receives sub-task specs (from LLM planning or ``spawn_agents`` tool)
  2. Resolves execution order (DAG → topological waves)
  3. Spawns and tracks sub-agents — now with **streaming scheduling**:
     agents start as soon as their dependencies complete, rather than
     waiting for the whole wave to finish.
  4. Supports REACTIVE MODE: master LLM reviews results and can
     spawn_more_agents, going through multiple review cycles
  5. Synthesises final results
  6. Auto-retries failed agents (configurable)

Key features (v2 — StreamingScheduler):
  • Streaming DAG scheduling — no wave barriers; agents launch as
    soon as their deps complete.
  • Reactive loop rewritten around StreamingScheduler + iter_completions
  • RateLimiter with exponential backoff on 429 / rate errors
  • resolve_execution_order uses Kahn's algorithm with cycle detection
  • Configurable fast-path: ``fast_path_enabled=True`` skips master
    review when all agents succeed with clean results (opt-in).
  • AsyncStreamingScheduler and async run_reactive for asyncio
  • Backward-compatible: _execute_wave, _retry_failed, run_swarm_task
    and all other public symbols still work as before.

Submodules (decomposed from this file):
  • planner.py — resolve_execution_order, plan_subtasks, _inject_dependency_context
  • synthesis.py — _build_synthesis_prompt, _synthesise
  • review.py — ReviewMixin (dashboard, review prompts, master review LLM call)
  • compat.py — spawn_sub_agent, run_swarm_task, _execute_wave, _retry_failed
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from lib.log import get_logger
from lib.swarm.agent import SubAgent
from lib.swarm.compat import (  # noqa: F401
    _STREAMING_SCHEDULER_MOVED,
    _execute_wave,
    _retry_failed,
    run_swarm_task,
    spawn_sub_agent,
)

# ── Re-exports from submodules (backward compatibility) ──
from lib.swarm.planner import (  # noqa: F401
    _inject_dependency_context,
    plan_subtasks,
    resolve_execution_order,
)
from lib.swarm.protocol import (
    ArtifactStore,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
)
from lib.swarm.rate_limiter import RateLimiter
from lib.swarm.review import ReviewMixin  # noqa: F401
from lib.swarm.scheduler import AsyncStreamingScheduler, StreamingScheduler
from lib.swarm.synthesis import (  # noqa: F401
    _build_synthesis_prompt,
    _synthesise,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  MasterOrchestrator — stateful wrapper with REACTIVE MODE
# ═══════════════════════════════════════════════════════════

class MasterOrchestrator(ReviewMixin):
    """Stateful orchestrator that supports reactive multi-round swarm execution.

    Modes:
      • FIRE-AND-FORGET: ``run()`` — executes all specs, returns results
      • REACTIVE: ``run_reactive()`` — uses ``StreamingScheduler`` for
        streaming DAG execution.  After each batch the master LLM
        reviews results and can ``spawn_more_agents``, going through
        multiple review cycles before final synthesis.

    IMPORTANT: By default the master review ALWAYS runs when the scheduler
    goes idle.  The review is essential for deciding whether additional
    investigation is needed.

    However, when ``fast_path_enabled=True``, the master review is SKIPPED
    for a batch where ALL agents succeeded and none of their results
    contain error indicators (e.g. "error", "failed", "exception").  This
    reduces latency and token usage for straightforward tasks.  The fast-
    path emits a ``FAST_PATH_SKIP`` event so the UI can display it.
    """

    def __init__(self, task_id: str, conv_id: str, specs: list,
                 project_path: str = '', project_enabled: bool = False,
                 model: str = '', thinking_enabled: bool = True,
                 thinking_depth: str = None,
                 search_mode: str = 'multi',
                 on_progress: Callable | None = None,
                 abort_check: Callable | None = None,
                 all_tools: list = None,
                 max_parallel: int = 8,
                 max_reactive_rounds: int = 5,
                 max_retries: int = 1,
                 fast_path_enabled: bool = False):
        self.task_id = task_id
        self.conv_id = conv_id
        self.specs = list(specs)
        self.project_path = project_path
        self.project_enabled = project_enabled
        self.model = model
        self.thinking_enabled = thinking_enabled
        self.thinking_depth = thinking_depth
        self.search_mode = search_mode
        self.on_progress = on_progress
        self.abort_check = abort_check or (lambda: False)
        self.all_tools = all_tools or []
        self.max_parallel = max_parallel
        self.max_reactive_rounds = max_reactive_rounds
        self.max_retries = max_retries
        self.fast_path_enabled = fast_path_enabled

        logger.info('[Master:%s] Init — %d specs, model=%s, thinking=%s, parallel=%d, '
                     'reactive_rounds=%d, retries=%d, fast_path=%s',
                     task_id, len(specs), model or '(default)', thinking_enabled,
                     max_parallel, max_reactive_rounds, max_retries, fast_path_enabled)
        for i, s in enumerate(specs):
            logger.debug('[Master:%s]   Spec[%d] id=%s role=%s deps=%s obj=%.120s',
                         task_id, i, s.id, s.role, list(s.depends_on or []),
                         s.objective)

        # Shared state
        self.artifact_store = ArtifactStore()
        self.rate_limiter = RateLimiter(max_concurrent=max_parallel)

        # Active sub-agents — populated during run()
        self._agents: dict[str, SubAgent] = {}
        self._results: list[tuple[SubTaskSpec, SubAgentResult]] = []
        self._results_by_id: dict[str, tuple[SubTaskSpec, SubAgentResult]] = {}
        self._lock = threading.Lock()
        self._aborted = False

        # Task proxy for sub-agents (must include events_lock / phase etc.)
        self._parent_task_proxy = {
            'id': task_id,
            'convId': conv_id,
            'events_lock': threading.Lock(),
            'events': [],
            'searchRounds': [],
            'phase': 'tool',
        }

        # Background thread pool for master review
        self._review_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='swarm-review')

    # ── Agent factory (used by StreamingScheduler) ───

    def _make_agent(self, spec: SubTaskSpec) -> SubAgent:
        """Create a SubAgent from a spec — used as factory for the scheduler."""
        logger.debug('[Master:%s] Creating SubAgent for spec=%s role=%s',
                     self.task_id, spec.id, spec.role)
        agent = spawn_sub_agent(
            spec,
            parent_task=self._parent_task_proxy,
            all_tools=self.all_tools,
            model=self.model,
            thinking_enabled=self.thinking_enabled,
            on_event=self.on_progress,
            abort_check=lambda: self._aborted or (
                self.abort_check() if self.abort_check else False),
            project_path=self.project_path,
            artifact_store=self.artifact_store,
        )
        with self._lock:
            self._agents[spec.id] = agent
        logger.debug('[Master:%s] SubAgent created: agent_id=%s model=%s', self.task_id, agent.agent_id, self.model)
        return agent

    # ── Record a batch of results ────────────────────

    def _record_batch(self, batch: list[tuple[SubTaskSpec, SubAgentResult]]):
        """Store a batch of (spec, result) pairs into the orchestrator state."""
        logger.debug('[Master:%s] Recording batch of %d result(s)', self.task_id, len(batch))
        for spec, result in batch:
            logger.debug('[Master:%s]   result id=%s role=%s status=%s answer_len=%d error=%s',
                         self.task_id, spec.id, spec.role, result.status,
                         len(result.final_answer or ''),
                         (result.error_message or '')[:120] if result.error_message else 'None')
            with self._lock:
                self._results.append((spec, result))
                self._results_by_id[spec.id] = (spec, result)

    # ── Scheduler callback helpers ───────────────────

    def _on_agent_start_callback(self, spec: SubTaskSpec):
        """Callback for scheduler on_agent_start."""
        if self.on_progress:
            self.on_progress({
                'type': 'swarm_agent_phase', 'phase': 'running',
                'content': f'▶️ Starting [{spec.role}]: {spec.objective[:60]}',
                'agentId': spec.id, 'role': spec.role,
                'objective': spec.objective[:200],
            })

    def _on_agent_complete_callback(self, spec: SubTaskSpec, result: SubAgentResult):
        """Callback for scheduler on_agent_complete."""
        if self.on_progress:
            self.on_progress({
                'type': 'swarm_agent_complete',
                'agentId': spec.id, 'role': spec.role,
                'objective': spec.objective[:200],
                'status': result.status,
                'elapsed': round(result.elapsed_seconds, 1),
                'tokens': result.total_tokens,
                'summary': (result.final_answer or '')[:200],
                'content': (
                    f'{"✅" if result.status == SubAgentStatus.COMPLETED.value else "❌"} '
                    f'[{spec.role}] Done in {result.elapsed_seconds:.1f}s'
                ),
            })

    def _on_retry_callback(self, spec: SubTaskSpec, attempt: int, err: str):
        """Callback for scheduler on_retry."""
        if self.on_progress:
            self.on_progress({
                'type': 'swarm_agent_phase', 'phase': 'retrying',
                'agentId': spec.id,
                'content': f'🔄 Retrying [{spec.role}] (attempt {attempt}): {err[:100]}',
            })

    def _build_scheduler(self) -> StreamingScheduler:
        """Build a StreamingScheduler with standard callbacks."""
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

    # ── Fire-and-forget run ──────────────────────────

    def run(self) -> list:
        """Execute all specs via StreamingScheduler, return list of (spec, result) tuples.

        This is the basic fire-and-forget mode — no reactive review.
        """
        scheduler = self._build_scheduler()

        try:
            scheduler.add_specs(self.specs)
            batch = scheduler.run_until_idle()
            self._record_batch(batch)
        finally:
            scheduler.shutdown()

        return self._results

    # ── Reactive run ─────────────────────────────────

    def run_reactive(self, original_query: str = '') -> str:
        """Execute with **true streaming reactive** loop.

        Unlike the old wave-barrier pattern, this method:
          1. Launches initial specs into StreamingScheduler
          2. Streams results via ``iter_completions()`` — the master
             can react after *each* agent completes, not after the
             entire wave
          3. Every ``review_interval`` completions (or when idle),
             the master LLM reviews and may spawn_more / swarm_done
          4. New specs are hot-injected into the *same* scheduler
             instance — they start immediately if deps are met,
             alongside still-running agents from previous batches.
          5. After max_reactive_rounds or swarm_done, synthesise.

        When ``fast_path_enabled`` is True on this orchestrator, the
        master review is skipped if the scheduler is idle and all
        results are clean (no error indicators).  Otherwise, the master
        review always runs.

        Returns the final synthesised answer string.
        """

        log_prefix = f'[Swarm-Reactive:{self.task_id}]'
        _t0_reactive = time.time()

        # ── Configurable: how many completions before a review ──
        review_interval = max(1, len(self.specs) // 2) if len(self.specs) > 2 else 1

        # Build a SINGLE scheduler that lives across all reactive rounds
        scheduler = self._build_scheduler()

        try:
            # ── Phase 1: Launch initial specs (non-blocking) ──
            logger.info('%s Phase 1: launching %d initial agent(s), max_parallel=%d',
                        log_prefix, len(self.specs), self.max_parallel)
            if self.on_progress:
                self.on_progress({
                    'type': 'swarm_phase', 'phase': 'executing',
                    'content': f'🚀 Executing {len(self.specs)} agent(s) (streaming)…',
                })
            scheduler.add_specs(self.specs)
            logger.debug('%s Phase 2: entering reactive streaming loop (review_interval=%d, max_rounds=%d)',
                        log_prefix, review_interval, self.max_reactive_rounds)

            # ── Phase 2: Streaming reactive loop ──
            since_last_review = 0
            reactive_rounds_used = 0
            master_said_done = False
            review_future: Future | None = None
            last_reviewed_index = 0  # watermark: results[:index] already reviewed

            def _process_review_decision(decision_type, new_specs):
                """Handle the result of a background master review."""
                nonlocal master_said_done, review_interval

                logger.info('%s _process_review_decision: type=%s new_specs=%d',
                            log_prefix, decision_type, len(new_specs) if new_specs else 0)

                if decision_type == 'swarm_done':
                    logger.info('%s Master decided: SWARM_DONE', log_prefix)
                    master_said_done = True
                    cancelled = scheduler.cancel_pending()
                    if cancelled:
                        logger.info(
                            '%s Cancelled %d pending specs after master said done',
                            log_prefix, len(cancelled))
                    return True

                elif decision_type == 'spawn_more' and new_specs:
                    if self.on_progress:
                        labels = [f'{s.role}: {s.objective[:50]}'
                                  for s in new_specs]
                        self.on_progress({
                            'type': 'swarm_phase', 'phase': 'spawn_more',
                            'agents': [
                                {'agentId': s.id, 'role': s.role,
                                 'objective': s.objective,
                                 'depends_on': s.depends_on or []}
                                for s in new_specs
                            ],
                            'content': (
                                f'🚀 Spawning {len(new_specs)} more agent(s) '
                                f'(injected into live scheduler):\n'
                                + '\n'.join(f'  • {l}' for l in labels)
                            ),
                        })
                    try:
                        scheduler.add_specs(new_specs)
                    except ValueError as e:
                        logger.warning(
                            '%s Failed to add specs: %s', log_prefix, e, exc_info=True)
                    total_agents = (scheduler.completed_count
                                    + scheduler.running_count
                                    + scheduler.pending_count)
                    review_interval = max(1, total_agents // 3)
                    return False

                else:
                    if scheduler.is_idle:
                        master_said_done = True
                        return True
                    return False

            for spec, result in scheduler.iter_completions():
                self._record_batch([(spec, result)])
                since_last_review += 1

                if self._aborted or self.abort_check():
                    break

                # ── Check if a background review has completed ──
                if review_future is not None and review_future.done():
                    try:
                        decision_type, new_specs = review_future.result()
                    except Exception as e:
                        logger.error(
                            '%s Background review failed: %s', log_prefix, e, exc_info=True)
                        decision_type, new_specs = ('continue', [])
                    review_future = None

                    should_break = _process_review_decision(
                        decision_type, new_specs)
                    if should_break:
                        break

                # Decide whether to trigger a master review now
                should_review = False
                if scheduler.is_idle:
                    should_review = True
                elif since_last_review >= review_interval:
                    should_review = True

                if not should_review:
                    continue

                # Don't launch a new review if one is already in flight
                if review_future is not None:
                    continue

                # ── Fast-path check ──
                if (scheduler.is_idle
                        and len(self._results) >= scheduler.completed_count
                        and self._check_fast_path_eligible(self._results)):
                    logger.info(
                        '%s Fast-path: all %d agents succeeded with clean results — skipping review',
                        log_prefix, len(self._results))
                    if self.on_progress:
                        self.on_progress({
                            'type': 'swarm_fast_path_skip',
                            'phase': 'fast_path',
                            'content': (
                                f'⚡ Fast-path: all {len(self._results)} '
                                f'agents succeeded — skipping master review'),
                        })
                    master_said_done = True
                    break

                if reactive_rounds_used >= self.max_reactive_rounds:
                    logger.info('%s Max reactive rounds reached', log_prefix)
                    break

                since_last_review = 0
                reactive_rounds_used += 1

                logger.info(
                    '%s Reactive review %d/%d (%d still running, %d pending)',
                    log_prefix, reactive_rounds_used, self.max_reactive_rounds,
                    scheduler.running_count, scheduler.pending_count)

                if self.on_progress:
                    still_running = scheduler.running_count
                    extra = (f' ({still_running} agent(s) still running)'
                             if still_running > 0 else '')
                    self.on_progress({
                        'type': 'swarm_phase', 'phase': 'reactive_review',
                        'content': (
                            f'🧠 Master reviewing results '
                            f'(round {reactive_rounds_used}){extra}…'),
                    })

                # ── Capture watermark and launch review in background ──
                review_watermark = last_reviewed_index
                last_reviewed_index = len(self._results)

                review_future = self._review_pool.submit(
                    self._master_review,
                    original_query, scheduler, log_prefix,
                    last_reviewed_index=review_watermark,
                )

            # ── Wait for any in-flight review to finish ──
            if review_future is not None and not review_future.done():
                logger.info(
                    '%s Waiting for in-flight review to complete…', log_prefix)
                try:
                    decision_type, new_specs = review_future.result(
                        timeout=120)
                    _process_review_decision(decision_type, new_specs)
                except Exception as e:
                    logger.error(
                        '%s In-flight review failed: %s', log_prefix, e, exc_info=True)
                review_future = None
            elif review_future is not None and review_future.done():
                try:
                    decision_type, new_specs = review_future.result()
                    _process_review_decision(decision_type, new_specs)
                except Exception as e:
                    logger.error(
                        '%s Final review failed: %s', log_prefix, e, exc_info=True)
                review_future = None

            # ── Drain remaining agents ──
            if not scheduler.is_idle and not (self._aborted or self.abort_check()):
                if self.on_progress:
                    self.on_progress({
                        'type': 'swarm_phase', 'phase': 'draining',
                        'content': (
                            f'⏳ Waiting for {scheduler.running_count} '
                            f'still-running agent(s) to finish…'),
                    })
                remaining_batch = scheduler.run_until_idle(timeout=300)
                self._record_batch(remaining_batch)

        finally:
            logger.info('%s Shutting down review pool and scheduler', log_prefix)
            # Shut down the review pool FIRST with wait=True so any in-flight
            # review finishes before we tear down the scheduler it depends on.
            # This prevents the background review thread from calling methods
            # (add_specs / cancel_pending) on an already-shut-down scheduler.
            self._review_pool.shutdown(wait=True)
            scheduler.shutdown()

        # ── Phase 3: Synthesis ──
        logger.info('%s Phase 3: synthesising final answer from %d result(s)',
                    log_prefix, len(self._results))
        dashboard = self._build_dashboard()
        logger.info('%s Dashboard:\n%s', log_prefix, dashboard)

        if self.on_progress:
            self.on_progress({
                'type': 'swarm_phase', 'phase': 'synthesis',
                'content': f'🔄 Synthesising final answer…\n\n{dashboard}',
            })

        synthesis_prompt = _build_synthesis_prompt(
            original_query, self._results, self.artifact_store
        )

        final_answer = _synthesise(
            synthesis_prompt, self.model,
            thinking_enabled=self.thinking_enabled,
            abort_check=lambda: self._aborted or self.abort_check(),
            on_event=self.on_progress,
        )

        # ── Report ──
        total_tokens = sum(r.total_tokens for _, r in self._results)
        total_cost = sum(r.cost_usd for _, r in self._results)
        failed = sum(1 for _, r in self._results
                     if r.status == SubAgentStatus.FAILED.value)
        retried = sum(1 for _, r in self._results if r.retry_count > 0)

        if self.on_progress:
            self.on_progress({
                'type': 'swarm_phase', 'phase': 'complete',
                'content': (
                    f'✅ Swarm complete — {len(self._results)} agents '
                    f'({failed} failed, {retried} retried), '
                    f'{total_tokens:,} tokens, ${total_cost:.4f}'),
                'agentCount': len(self._results),
                'failedCount': failed,
                'totalTokens': total_tokens,
                'totalCost': round(total_cost, 4),
                'agents': [
                    {'agentId': s.id, 'role': s.role,
                     'objective': (s.objective or '')[:200],
                     'status': r.status,
                     'elapsed': round(r.elapsed_seconds, 1),
                     'tokens': r.total_tokens,
                     'summary': (r.final_answer or '')[:200]}
                    for s, r in self._results
                ],
            })

        _elapsed_total = time.time() - _t0_reactive
        logger.info(
            '[Swarm:%s] ═══ COMPLETE ═══ agents=%d failed=%d retried=%d '
            'tokens=%s cost=$%.4f elapsed=%.1fs',
            self.task_id, len(self._results), failed, retried,
            f'{total_tokens:,}', total_cost, _elapsed_total)

        for spec, result in self._results:
            logger.debug(
                '[Swarm:%s]   agent=%s role=%s status=%s elapsed=%.1fs tokens=%d rounds=%d',
                self.task_id, spec.id, spec.role, result.status,
                result.elapsed_seconds, result.total_tokens, result.rounds_used)

        return final_answer

    # ── Helper: execute additional specs (backward compat) ──

    def _execute_additional_specs(self, new_specs: list[SubTaskSpec]):
        """Execute additional specs (from spawn_more_agents).

        Kept for backward compatibility with integration.py calling
        this directly.  Internally uses StreamingScheduler.
        """
        scheduler = StreamingScheduler(
            agent_factory=self._make_agent,
            rate_limiter=self.rate_limiter,
            max_parallel=self.max_parallel,
            abort_check=lambda: self._aborted or self.abort_check(),
            default_retries=self.max_retries,
        )

        # Seed the scheduler with already-completed results so dependency
        # injection works for the new specs.
        with self._lock:
            for sid, (spec, result) in self._results_by_id.items():
                scheduler._completed[sid] = (spec, result)

        try:
            scheduler.add_specs(new_specs)
            batch = scheduler.run_until_idle()
            self._record_batch(batch)
        finally:
            scheduler.shutdown()

    # ── Helper: spawn a wave of agents (backward compat) ──

    def _spawn_wave(self, wave: list[SubTaskSpec]) -> list[tuple[SubTaskSpec, SubAgent]]:
        """Spawn agents for a wave.

        Kept for backward compatibility.
        """
        agents = []
        for spec in wave:
            agent = self._make_agent(spec)
            agents.append((spec, agent))
        return agents

    # ── Status ───────────────────────────────────────

    def get_status(self) -> dict:
        """Return per-agent status dict for the check_agents tool."""
        with self._lock:
            out = {}
            if self._agents:
                for sid, agent in self._agents.items():
                    info = {
                        'role': agent.spec.role,
                        'objective': agent.spec.objective[:120],
                        'status': (agent.result.status
                                   if agent.result else 'unknown'),
                        'round': (agent.result.rounds_used
                                  if agent.result else 0),
                        'max_rounds': getattr(agent, 'max_rounds', 0),
                        'last_action': (
                            agent.result.tool_log[-1]
                            if agent.result and agent.result.tool_log
                            else ''),
                    }
                    out[sid] = info
            else:
                for spec in self.specs:
                    out[spec.id] = {
                        'role': spec.role,
                        'objective': spec.objective[:120],
                        'status': 'pending',
                        'round': 0,
                        'max_rounds': 0,
                        'last_action': '',
                    }
            return out

    # ── Artifacts ────────────────────────────────────

    def get_artifacts(self) -> dict:
        """Return all shared artifacts."""
        return self.artifact_store.get_all()

    # ── Abort ────────────────────────────────────────

    def abort(self):
        """Signal all sub-agents to stop."""
        self._aborted = True

    # ── Async reactive run ───────────────────────────

    async def run_reactive_async(self, original_query: str = '') -> str:
        """Async version of ``run_reactive()`` using ``AsyncStreamingScheduler``.

        Uses ``asyncio.to_thread`` to wrap the synchronous LLM client
        (``stream_chat`` / ``build_body``) as an intermediate step toward
        a fully async LLM client.  All scheduler operations (agent
        factory, review, synthesis) are offloaded to threads so the
        asyncio event loop stays responsive.

        The interface and behavior are identical to ``run_reactive()``,
        including fast-path support.

        Returns the final synthesised answer string.
        """
        import asyncio

        log_prefix = f'[Swarm-AsyncReactive:{self.task_id}]'

        # ── Build async scheduler ──
        async_scheduler = AsyncStreamingScheduler(
            agent_factory=self._make_agent,
            rate_limiter=self.rate_limiter,
            max_parallel=self.max_parallel,
            abort_check=lambda: self._aborted or self.abort_check(),
            default_retries=self.max_retries,
        )

        review_interval = max(1, len(self.specs) // 2) if len(self.specs) > 2 else 1

        try:
            # ── Phase 1: Launch initial specs ──
            if self.on_progress:
                self.on_progress({
                    'type': 'swarm_phase', 'phase': 'executing',
                    'content': f'🚀 Executing {len(self.specs)} agent(s) (async streaming)…',
                })
            await async_scheduler.add_specs(self.specs)

            # ── Phase 2: Async reactive loop ──
            since_last_review = 0
            reactive_rounds_used = 0
            master_said_done = False
            last_reviewed_index = 0

            async for spec, result in async_scheduler.iter_completions():
                self._record_batch([(spec, result)])
                since_last_review += 1

                if self._aborted or self.abort_check():
                    break

                # Decide whether to trigger review
                should_review = False
                if async_scheduler.is_idle:
                    should_review = True
                elif since_last_review >= review_interval:
                    should_review = True

                if not should_review:
                    continue

                # ── Fast-path check ──
                if (async_scheduler.is_idle
                        and len(self._results) >= async_scheduler.completed_count
                        and self._check_fast_path_eligible(self._results)):
                    logger.info(
                        '%s Fast-path: all %d agents succeeded — skipping review',
                        log_prefix, len(self._results))
                    if self.on_progress:
                        self.on_progress({
                            'type': 'swarm_fast_path_skip',
                            'phase': 'fast_path',
                            'content': (
                                f'⚡ Fast-path: all {len(self._results)} '
                                f'agents succeeded — skipping master review'),
                        })
                    master_said_done = True
                    break

                if reactive_rounds_used >= self.max_reactive_rounds:
                    logger.info('%s Max reactive rounds reached', log_prefix)
                    break

                since_last_review = 0
                reactive_rounds_used += 1

                if self.on_progress:
                    self.on_progress({
                        'type': 'swarm_phase', 'phase': 'reactive_review',
                        'content': (
                            f'🧠 Master reviewing results '
                            f'(round {reactive_rounds_used})…'),
                    })

                # ── Master review in thread ──
                review_watermark = last_reviewed_index
                last_reviewed_index = len(self._results)

                decision_type, new_specs = await asyncio.to_thread(
                    self._master_review,
                    original_query, async_scheduler._sync_scheduler,
                    log_prefix,
                    last_reviewed_index=review_watermark,
                )

                if decision_type == 'swarm_done':
                    master_said_done = True
                    await async_scheduler.cancel_pending()
                    break
                elif decision_type == 'spawn_more' and new_specs:
                    if self.on_progress:
                        labels = [f'{s.role}: {s.objective[:50]}'
                                  for s in new_specs]
                        self.on_progress({
                            'type': 'swarm_phase', 'phase': 'spawn_more',
                            'content': (
                                f'🚀 Spawning {len(new_specs)} more agent(s):\n'
                                + '\n'.join(f'  • {l}' for l in labels)),
                        })
                    await async_scheduler.add_specs(new_specs)
                    review_interval = max(1, (
                        async_scheduler.completed_count
                        + async_scheduler.running_count
                        + async_scheduler.pending_count) // 3)
                else:
                    if async_scheduler.is_idle:
                        master_said_done = True
                        break

            # ── Drain remaining ──
            if not async_scheduler.is_idle and not (self._aborted or self.abort_check()):
                remaining = await async_scheduler.run_until_idle()
                self._record_batch(remaining)

        finally:
            async_scheduler.shutdown()

        # ── Phase 3: Synthesis (in thread) ──
        if self.on_progress:
            self.on_progress({
                'type': 'swarm_phase', 'phase': 'synthesis',
                'content': '🔄 Synthesising final answer…',
            })

        synthesis_prompt = _build_synthesis_prompt(
            original_query, self._results, self.artifact_store
        )

        final_answer = await asyncio.to_thread(
            _synthesise,
            synthesis_prompt, self.model,
            thinking_enabled=self.thinking_enabled,
            abort_check=lambda: self._aborted or self.abort_check(),
            on_event=self.on_progress,
        )

        if self.on_progress:
            total_tokens = sum(r.total_tokens for _, r in self._results)
            total_cost = sum(r.cost_usd for _, r in self._results)
            failed = sum(1 for _, r in self._results
                         if r.status == SubAgentStatus.FAILED.value)
            self.on_progress({
                'type': 'swarm_phase', 'phase': 'complete',
                'content': f'✅ Swarm complete — {len(self._results)} agents, '
                           f'{total_tokens:,} tokens',
                'agentCount': len(self._results),
                'failedCount': failed,
                'totalTokens': total_tokens,
                'totalCost': round(total_cost, 4),
                'agents': [
                    {'agentId': s.id, 'role': s.role,
                     'objective': (s.objective or '')[:200],
                     'status': r.status,
                     'elapsed': round(r.elapsed_seconds, 1),
                     'tokens': r.total_tokens,
                     'summary': (r.final_answer or '')[:200]}
                    for s, r in self._results
                ],
            })

        return final_answer
