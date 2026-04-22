"""lib/swarm/compat.py — Backward-compatible swarm API wrappers.

**This module has NOTHING to do with** :mod:`lib.compat` (cross-platform
helpers for Windows/macOS/Linux shell, pipe I/O, /proc). The name
collision is historical — this file predates the cross-platform module.
Here, "compat" means "API backward compatibility" for swarm callers
that existed before :class:`~lib.swarm.scheduler.StreamingScheduler`.

Extracted from the old ``master.py``:
  • :func:`spawn_sub_agent` — create a :class:`~lib.swarm.agent.SubAgent`
    from a :class:`~lib.swarm.protocol.SubTaskSpec`
  • :func:`_execute_wave` — execute a wave of agents in parallel
    (pre-StreamingScheduler path)
  • :func:`_retry_failed` — retry failed agents with error context
  • :func:`run_swarm_task` — fire-and-forget functional API
  • ``_STREAMING_SCHEDULER_MOVED`` — sentinel for backward compat

New code should prefer :class:`~lib.swarm.scheduler.StreamingScheduler` /
:class:`~lib.swarm.master.MasterOrchestrator` directly.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.log import get_logger
from lib.swarm.agent import SubAgent
from lib.swarm.planner import plan_subtasks
from lib.swarm.protocol import (
    ArtifactStore,
    SubAgentResult,
    SubAgentStatus,
    SubTaskSpec,
)
from lib.swarm.rate_limiter import RateLimiter
from lib.swarm.scheduler import StreamingScheduler
from lib.swarm.synthesis import _build_synthesis_prompt, _synthesise

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Sentinel
# ═══════════════════════════════════════════════════════════

_STREAMING_SCHEDULER_MOVED = True  # StreamingScheduler is now in lib.swarm.scheduler


# ═══════════════════════════════════════════════════════════
#  Sub-Agent Spawning
# ═══════════════════════════════════════════════════════════

def spawn_sub_agent(spec: SubTaskSpec, *,
                    parent_task: dict,
                    all_tools: list,
                    model: str = '',
                    thinking_enabled: bool = True,
                    on_event: Callable | None = None,
                    abort_check: Callable | None = None,
                    project_path: str = '',
                    artifact_store: ArtifactStore | None = None) -> SubAgent:
    """Create a SubAgent instance from a spec. Does NOT start execution."""
    logger.info('[Swarm-Spawn] Creating SubAgent id=%s role=%s model=%s objective=%.80s deps=%s',
                spec.id, spec.role, model or '(default)', spec.objective, spec.depends_on or [])
    return SubAgent(
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


# ═══════════════════════════════════════════════════════════
#  Wave Execution Helpers (backward-compatible)
# ═══════════════════════════════════════════════════════════

def _execute_wave(agents: list[tuple[SubTaskSpec, SubAgent]],
                  rate_limiter: 'RateLimiter',
                  max_parallel: int,
                  abort_check: Callable | None = None,
                  ) -> list[tuple[SubTaskSpec, SubAgentResult]]:
    """Execute a wave of agents in parallel with rate limiting.

    Kept for backward compatibility — new code should prefer
    ``StreamingScheduler``.
    """
    results = []

    if len(agents) == 1:
        spec, agent = agents[0]
        result = rate_limiter.run_agent(agent)
        results.append((spec, result))
    else:
        with ThreadPoolExecutor(
            max_workers=min(len(agents), max_parallel),
            thread_name_prefix='swarm',
        ) as pool:
            futures = {}
            for spec, agent in agents:
                future = pool.submit(rate_limiter.run_agent, agent)
                futures[future] = (spec, agent)

            for future in as_completed(futures):
                spec, agent = futures[future]
                try:
                    result = future.result(timeout=600)
                except Exception as e:
                    logger.warning('Agent %s exception: %s', spec.id, e, exc_info=True)
                    result = SubAgentResult(
                        status=SubAgentStatus.FAILED.value,
                        error_message=f'{type(e).__name__}: {e}',
                    )
                results.append((spec, result))

    return results


def _retry_failed(wave_results: list[tuple[SubTaskSpec, SubAgentResult]],
                  task: dict, all_tools: list,
                  model: str, thinking_enabled: bool,
                  on_event: Callable | None,
                  abort_check: Callable | None,
                  project_path: str,
                  artifact_store: ArtifactStore,
                  rate_limiter: 'RateLimiter',
                  max_retries: int,
                  ) -> list[tuple[SubTaskSpec, SubAgentResult]]:
    """Retry failed agents with error context injected.

    Kept for backward compatibility — ``StreamingScheduler._run_one``
    handles retries internally.
    """
    final_results = []

    for spec, result in wave_results:
        effective_retries = spec.max_retries if spec.max_retries > 0 else max_retries
        retry_count = 0

        while (result.status == SubAgentStatus.FAILED.value
               and retry_count < effective_retries
               and not (abort_check and abort_check())):

            retry_count += 1
            logger.info('[Swarm] Retrying agent %s (%s), attempt %d/%d: %s',
                        spec.id, spec.role, retry_count, effective_retries,
                        result.error_message[:100])

            if on_event:
                on_event({'type': 'swarm_agent_retry',
                          'agent_id': spec.id,
                          'content': f'🔄 Retrying [{spec.role}] (attempt {retry_count}): '
                                     f'{result.error_message[:100]}'})

            retry_spec = SubTaskSpec(
                role=spec.role,
                objective=spec.objective,
                context=(spec.context +
                         f'\n\n⚠️ Previous attempt failed with: {result.error_message}\n'
                         f'Please try a different approach.'),
                depends_on=[],
                id=f'{spec.id}-retry{retry_count}',
                priority=spec.priority,
                max_rounds=spec.max_rounds,
                tools_hint=spec.tools_hint,
                max_retries=0,
                model_override=spec.model_override,
            )

            retry_agent = spawn_sub_agent(
                retry_spec, parent_task=task, all_tools=all_tools,
                model=model, thinking_enabled=thinking_enabled,
                on_event=on_event, abort_check=abort_check,
                project_path=project_path,
                artifact_store=artifact_store,
            )

            result = rate_limiter.run_agent(retry_agent)
            result.retry_count = retry_count

        final_results.append((spec, result))

    return final_results


# ═══════════════════════════════════════════════════════════
#  Functional API — run_swarm_task
# ═══════════════════════════════════════════════════════════

def run_swarm_task(task: dict, user_query: str, *,
                   all_tools: list,
                   system_prompt_base: str = '',
                   model: str = '',
                   thinking_enabled: bool = True,
                   project_path: str = '',
                   abort_check: Callable | None = None,
                   on_event: Callable | None = None,
                   max_parallel: int = 8,
                   max_retries: int = 1) -> str:
    """Execute a complex task using the swarm pattern (fire-and-forget).

    This is the main entry point called from the orchestrator when
    swarm mode is activated.

    Steps:
      1. Plan subtasks (LLM-based decomposition)
      2. Execute via StreamingScheduler (dependency-aware streaming)
      3. Auto-retry failed agents
      4. Synthesise results
    """
    log_prefix = f'[Swarm:{task.get("id", "?")}]'
    logger.info('%s Starting swarm for: %s', log_prefix, user_query[:100])

    if on_event:
        on_event({'type': 'swarm_phase', 'phase': 'planning',
                  'content': '🧠 Planning subtasks...'})

    # ── Phase 1: Plan ──
    specs = plan_subtasks(
        user_query, model=model,
        thinking_enabled=thinking_enabled,
        abort_check=abort_check,
        on_event=on_event,
    )

    if on_event:
        labels = [f'{s.role}: {s.objective[:50]}' for s in specs]
        on_event({'type': 'swarm_phase', 'phase': 'spawning',
                  'content': f'🚀 Spawning {len(specs)} agents:\n' +
                             '\n'.join(f'  • {l}' for l in labels),
                  'agents': [
                      {'agentId': s.id, 'role': s.role,
                       'objective': s.objective[:120]}
                      for s in specs
                  ]})

    # ── Phase 2: Execute via StreamingScheduler ──
    artifact_store = ArtifactStore()
    rate_limiter = RateLimiter(max_concurrent=max_parallel)

    def make_agent(spec: SubTaskSpec) -> SubAgent:
        return spawn_sub_agent(
            spec, parent_task=task, all_tools=all_tools,
            model=model, thinking_enabled=thinking_enabled,
            on_event=on_event, abort_check=abort_check,
            project_path=project_path,
            artifact_store=artifact_store,
        )

    scheduler = StreamingScheduler(
        agent_factory=make_agent,
        rate_limiter=rate_limiter,
        max_parallel=max_parallel,
        abort_check=abort_check,
        default_retries=max_retries,
        on_agent_start=lambda spec: (
            on_event({'type': 'swarm_agent_phase', 'phase': 'running',
                      'content': f'▶️ Starting [{spec.role}]: {spec.objective[:60]}',
                      'agentId': spec.id, 'role': spec.role,
                      'objective': spec.objective[:120]})
            if on_event else None
        ),
    )

    try:
        scheduler.add_specs(specs)
        scheduler.run_until_idle()

        all_results = scheduler.all_results

        # ── Phase 3: Synthesise ──
        if on_event:
            on_event({'type': 'swarm_phase', 'phase': 'synthesis',
                      'content': '🔄 Synthesising results...'})

        synthesis_prompt = _build_synthesis_prompt(user_query, all_results, artifact_store)
        final_answer = _synthesise(
            synthesis_prompt, model,
            thinking_enabled=thinking_enabled,
            abort_check=abort_check,
            on_event=on_event,
        )

        # ── Report ──
        total_tokens = sum(r.total_tokens for _, r in all_results)
        total_cost = sum(r.cost_usd for _, r in all_results)
        failed = sum(1 for _, r in all_results
                     if r.status == SubAgentStatus.FAILED.value)

        if on_event:
            on_event({
                'type': 'swarm_phase', 'phase': 'complete',
                'content': (f'✅ Swarm complete — {len(all_results)} agents, '
                            f'{failed} failed, '
                            f'{total_tokens:,} tokens, ${total_cost:.4f}'),
                'agentCount': len(all_results),
                'failedCount': failed,
                'totalTokens': total_tokens,
                'totalCost': round(total_cost, 4),
                'agents': [
                    {'agentId': s.id, 'role': s.role,
                     'objective': (s.objective or '')[:120],
                     'status': r.status,
                     'elapsed': round(r.elapsed_seconds, 1),
                     'tokens': r.total_tokens,
                     'summary': (r.final_answer or '')[:200]}
                    for s, r in all_results
                ]})

        logger.info('%s Complete: %d agents, %s total tokens, $%.4f',
                    log_prefix, len(all_results), f'{total_tokens:,}', total_cost)

        return final_answer

    finally:
        scheduler.shutdown()
