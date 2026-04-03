"""lib/swarm/planner.py — DAG resolution and LLM-based task decomposition.

Extracted from master.py:
  • resolve_execution_order() — Kahn's algorithm topological sort
  • plan_subtasks() — LLM-based task decomposition into SubTaskSpecs
  • _inject_dependency_context() — inject completed dep results into spec context
"""

import json
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Optional

from lib.llm_client import build_body
from lib.llm_dispatch import dispatch_stream as _dispatch_stream
from lib.log import get_logger
from lib.swarm.result_format import compress_result
from lib.swarm.types import SubTaskSpec

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Execution Order — DAG → topological wave sort (Kahn's)
# ═══════════════════════════════════════════════════════════

def resolve_execution_order(specs: list[SubTaskSpec]) -> list[list[SubTaskSpec]]:
    """Sort specs into waves based on dependency DAG (topological sort).

    Uses Kahn's algorithm with **explicit cycle detection**.  When a cycle
    is found a ``ValueError`` is raised listing the involved IDs, instead
    of silently forcing them into one wave.

    Returns a list of waves, where each wave is a list of specs that
    can be executed in parallel (all their dependencies are in earlier waves).
    """
    id_to_spec = {s.id: s for s in specs}
    in_degree: dict[str, int] = {}
    dependents: dict[str, list[str]] = defaultdict(list)

    for s in specs:
        in_degree[s.id] = 0
    for s in specs:
        for dep_id in (s.depends_on or []):
            if dep_id in id_to_spec:
                in_degree[s.id] += 1
                dependents[dep_id].append(s.id)

    waves: list[list[SubTaskSpec]] = []
    remaining = set(id_to_spec.keys())

    logger.info('[Swarm-DAG] Resolving execution order for %d specs: %s',
                len(specs), [(s.id, s.role) for s in specs])
    for s in specs:
        deps = list(s.depends_on or [])
        logger.debug('[Swarm-DAG]   spec %s (role=%s) objective=%.80s depends_on=%s priority=%d',
                     s.id, s.role, s.objective, deps or '(none)', s.priority)

    while remaining:
        # Find all specs with no unresolved dependencies
        wave_ids = [sid for sid in remaining if in_degree[sid] == 0]
        if not wave_ids:
            # ── Cycle detected — report it clearly ──
            cycle_ids = sorted(remaining)
            logger.error('[Swarm-DAG] Cycle detected! Involved IDs: %s', cycle_ids)
            raise ValueError(
                f'Cycle detected in dependency graph — cannot schedule. '
                f'Involved IDs: {cycle_ids}'
            )

        # Sort within wave by priority (higher first)
        wave_ids.sort(key=lambda sid: id_to_spec[sid].priority, reverse=True)
        wave = [id_to_spec[sid] for sid in wave_ids]
        waves.append(wave)
        logger.debug('[Swarm-DAG] Wave %d: %s', len(waves),
                     [(s.id, s.role, s.objective[:50]) for s in wave])

        for sid in wave_ids:
            remaining.discard(sid)
            for dep in dependents[sid]:
                in_degree[dep] -= 1

    logger.info('[Swarm-DAG] Resolved %d specs into %d waves', len(specs), len(waves))
    return waves


# ═══════════════════════════════════════════════════════════
#  Plan Subtasks (LLM-based planning)
# ═══════════════════════════════════════════════════════════

def plan_subtasks(user_query: str, *, model: str = '',
                  thinking_enabled: bool = True,
                  thinking_depth: str = None,
                  abort_check: Callable | None = None,
                  on_event: Callable | None = None) -> list[SubTaskSpec]:
    """Ask the LLM to decompose a user query into sub-tasks.

    Returns a list of SubTaskSpec that can be fed to
    ``run_swarm_task()`` or ``MasterOrchestrator``.
    """
    logger.info('[Swarm-Plan] Planning subtasks for query (len=%d): %.120s',
                len(user_query), user_query)
    logger.debug('[Swarm-Plan] Planning params: model=%s, thinking=%s', model, thinking_enabled)

    planning_prompt = f"""Split this task into 2-6 sub-tasks that can run IN PARALLEL for maximum speed.

For each sub-task provide:
- objective: specific, actionable description of what to do
- context: any relevant context (optional)
- id: short unique ID (e.g. "t1", "t2")

Only add depends_on (list of IDs) if a task truly cannot start without another's output. Maximize parallelism.

Respond with a JSON array. Example:
[{{"id":"t1","objective":"Search for X"}},{{"id":"t2","objective":"Analyze Y"}}]

Task: {user_query}"""

    messages = [
        {'role': 'system', 'content': 'You split tasks into parallel sub-tasks for speed. Respond with JSON only. No roles needed — just objectives.'},
        {'role': 'user', 'content': planning_prompt},
    ]

    body = build_body(model=model, messages=messages,
                      tools=None, max_tokens=8000,
                      thinking_enabled=thinking_enabled,
                      thinking_depth=thinking_depth,
                      temperature=0.7)

    content_parts: list[str] = []

    def on_content(chunk):
        content_parts.append(chunk)

    msg, _, _ = _dispatch_stream(body, on_content=on_content,
                                abort_check=abort_check,
                                prefer_model=body.get('model', ''),
                                log_prefix='[Swarm-Plan]')
    raw = msg.get('content', ''.join(content_parts))

    # Parse JSON from response
    specs: list[SubTaskSpec] = []
    try:
        text = raw.strip()
        if '```' in text:
            for block in text.split('```'):
                block = block.strip()
                if block.startswith('json'):
                    block = block[4:].strip()
                if block.startswith('['):
                    text = block
                    break
        try:
            if text.startswith('['):
                items = json.loads(text)
            else:
                start = text.index('[')
                end = text.rindex(']') + 1
                items = json.loads(text[start:end])
        except (json.JSONDecodeError, ValueError) as e:
            logger.error('[Planner] Failed to parse LLM plan as JSON: %s — raw[:200]=%r', e, text[:200], exc_info=True)
            raise ValueError(f'Plan JSON parse failed: {e}') from e

        for item in items:
            spec = SubTaskSpec(
                role=item.get('role', 'general'),
                objective=item.get('objective', ''),
                context=item.get('context', ''),
                depends_on=item.get('depends_on', []),
                id=item.get('id', str(uuid.uuid4())[:8]),
                max_retries=item.get('max_retries', 1),
                model_override=item.get('model_override', ''),
            )
            specs.append(spec)
    except (json.JSONDecodeError, ValueError, TypeError, KeyError) as e:
        logger.warning('[Swarm-Plan] Failed to parse subtask plan: %s', e, exc_info=True)
        specs = [SubTaskSpec(role='general', objective=user_query, id='t1')]

    logger.info('[Swarm-Plan] Planned %d subtasks: %s',
                len(specs), [s.role + ': ' + s.objective[:40] for s in specs])

    return specs


# ═══════════════════════════════════════════════════════════
#  Dependency Context Injection
# ═══════════════════════════════════════════════════════════

def _inject_dependency_context(spec: SubTaskSpec,
                               results_by_id: dict) -> None:
    """Inject results from completed dependencies into a spec's context."""
    if not spec.depends_on:
        return
    dep_results = []
    for dep_id in spec.depends_on:
        if dep_id in results_by_id:
            dep_spec, dep_result = results_by_id[dep_id]
            dep_results.append(
                f'[{dep_spec.role}] {dep_spec.objective[:80]}:\n'
                f'{compress_result(dep_result.final_answer, max_chars=2000)}'
            )
    if dep_results:
        spec.context += (
            '\n\nResults from prerequisite tasks:\n'
            + '\n---\n'.join(dep_results)
        )
