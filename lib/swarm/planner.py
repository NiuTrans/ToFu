"""lib/swarm/planner.py — DAG resolution for swarm specs.

Contains exactly one public function:

  • ``resolve_execution_order`` — Kahn's algorithm topological sort with
    explicit cycle detection. Used by ``StreamingScheduler`` to validate
    new specs before they're added to the live scheduler.

Historical note: this module used to also expose ``plan_subtasks`` (an
LLM-based decomposer) and ``_inject_dependency_context`` (which mutated a
spec's ``context`` to include upstream results). Both were removed when
the async swarm refactor dropped the synchronous ``run_swarm_task`` /
master-review path — the main agent now decomposes its own task and
sub-agents share data via ``ArtifactStore`` instead of injected context
strings.
"""

from collections import defaultdict

from lib.log import get_logger
from lib.swarm.types import SubTaskSpec

logger = get_logger(__name__)


def resolve_execution_order(specs: list[SubTaskSpec]) -> list[list[SubTaskSpec]]:
    """Sort specs into waves based on dependency DAG (topological sort).

    Uses Kahn's algorithm with explicit cycle detection. When a cycle is
    found a ``ValueError`` is raised listing the involved IDs, instead of
    silently forcing them into one wave.

    Returns a list of waves, where each wave is a list of specs that can be
    executed in parallel (all their dependencies are in earlier waves).
    """
    id_to_spec = {s.id: s for s in specs}
    in_degree: dict[str, int] = {s.id: 0 for s in specs}
    dependents: dict[str, list[str]] = defaultdict(list)

    for s in specs:
        for dep_id in (s.depends_on or []):
            if dep_id in id_to_spec:
                in_degree[s.id] += 1
                dependents[dep_id].append(s.id)

    waves: list[list[SubTaskSpec]] = []
    remaining = set(id_to_spec.keys())

    logger.info('[Swarm-DAG] Resolving execution order for %d specs', len(specs))

    while remaining:
        wave_ids = [sid for sid in remaining if in_degree[sid] == 0]
        if not wave_ids:
            cycle_ids = sorted(remaining)
            logger.error('[Swarm-DAG] Cycle detected! Involved IDs: %s', cycle_ids)
            raise ValueError(
                f'Cycle detected in dependency graph — cannot schedule. '
                f'Involved IDs: {cycle_ids}'
            )

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
