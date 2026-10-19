"""Maintenance cycle: the one heartbeat of the self-evolution system.

Responsibility: run every learning pass in dependency order, so the system
has a single orchestration point instead of N ad-hoc calls. Entry point:
``run_maintenance_cycle``. Dependencies: every learning module; order is the
contract — priors before hypothesis validation (predictions consume priors),
stores before crystallization (recurrence reads settled episodes), friction
mining after gap collection and before curriculum (the frontier reads
gaps/hypotheses/anti-patterns/observations), and mutation monitoring last
(it judges everything that settled since its last activation).

Each step is fail-soft and reported individually: a failing pass degrades
its own output, never the cycle. This is what makes the module a system
rather than a pile of parts.
"""
from __future__ import annotations

import time

from lib.log import get_logger

from .cost_model import recompute_priors
from .episodes import episode_read_window
from .crystallize import (
    propose_flow_template_crystallizations,
    propose_macro_crystallizations,
)
from .curriculum import propose_probes
from .friction import mine_friction
from .gate import collect_capability_gaps, recover_policy_activations
from .holdout import build_holdout_suite, monitor_active_mutations
from .observations import propose_frame_changes
from .refetch import reindex_maintenance
from .self_model import validate_hypotheses
from .strategies import distill_strategies

logger = get_logger(__name__)

_CYCLE = (
    ('mutation_recovery', lambda uid, episode_reader=None: recover_policy_activations(user_id=uid)),
    ('priors', lambda uid, episode_reader=None: [r['domain'] for r in recompute_priors(
        user_id=uid, episode_reader=episode_reader, goal_reader=episode_reader.read_goal)]),
    ('hypotheses', lambda uid, episode_reader=None: [
        f"{h['hypothesis_id']}:{h['status']}"
        for h in validate_hypotheses(user_id=uid, episode_reader=episode_reader, goal_reader=episode_reader.read_goal)]),
    ('strategies', lambda uid, episode_reader=None: {
        k: len(v) for k, v in distill_strategies(user_id=uid).items()}),
    ('capability_gaps', lambda uid, episode_reader=None: len(
        collect_capability_gaps(user_id=uid, episode_reader=episode_reader))),
    ('friction', lambda uid, episode_reader=None: len(mine_friction(user_id=uid, episode_reader=episode_reader))),
    ('refetch', lambda uid, episode_reader=None: reindex_maintenance(user_id=uid)),
    ('frame_proposals', lambda uid, episode_reader=None: propose_frame_changes(user_id=uid)),
    ('holdout', lambda uid, episode_reader=None: len(build_holdout_suite(user_id=uid, episode_reader=episode_reader, goal_reader=episode_reader.read_goal))),
    ('crystallizations', lambda uid, episode_reader=None: len(
        propose_macro_crystallizations(user_id=uid, episode_reader=episode_reader))),
    ('flow_crystallizations', lambda uid, episode_reader=None: len(
        propose_flow_template_crystallizations(user_id=uid, episode_reader=episode_reader))),
    ('curriculum', lambda uid, episode_reader=None: len(propose_probes(user_id=uid, episode_reader=episode_reader, goal_reader=episode_reader.read_goal))),
    ('monitoring', lambda uid, episode_reader=None: monitor_active_mutations(user_id=uid, episode_reader=episode_reader, goal_reader=episode_reader.read_goal)),
)


def run_maintenance_cycle(*, user_id, now: int | None = None,
                          include_refetch: bool = True) -> dict:
    """Run learning passes; terminal settlement defers optional source retrieval.

    Explicit maintenance retains all passes. A terminal receiver must deliver
    durable acceptance without waiting for unrelated academic/network retries.
    Deferred retrieval remains available through the budgeted executor tool or
    the owner's explicit maintenance request; no background worker is created.
    """
    del now  # deterministic ordering is the contract; wall time is not
    started = time.time()
    steps = {}
    with episode_read_window(user_id=user_id) as episode_reader:
        for name, step in _CYCLE:
            if name == 'refetch' and not include_refetch:
                steps[name] = {'ok': True, 'result': {
                    'deferred': True,
                    'reason': 'Source retrieval requires budgeted evolution_reindex execution or an explicit maintenance request.',
                }}
                continue
            try:
                steps[name] = {'ok': True, 'result': step(user_id, episode_reader)}
            except Exception as exc:
                logger.warning('[Evolution:Maintenance] step %s failed: %s',
                               name, exc)
                steps[name] = {'ok': False, 'error': str(exc)[:300]}
    return {
        'steps': steps,
        'ok': all(s['ok'] for s in steps.values()),
        'complete': include_refetch,
        'elapsed_seconds': round(time.time() - started, 3),
    }
