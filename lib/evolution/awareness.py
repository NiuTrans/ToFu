"""System awareness: one bounded snapshot of what the system knows.

Responsibility: answer "what does the evolution system know right now" for
every surface — the workbench header, API clients, and (later) ambient
context — so modules and humans perceive the same system state instead of
each reading raw stores. Entry point: ``system_snapshot``. Dependencies:
all stores, read-only, every list capped by construction.
"""
from __future__ import annotations

from .cost_model import list_priors
from .episodes import list_episodes
from .gate import list_capability_gaps, list_mutations
from .observations import cluster_observations, list_observations
from .self_model import list_hypotheses
from .skill_forge import list_forged_skills
from .strategies import list_anti_patterns, list_strategies

_CAP = 6


def system_snapshot(*, user_id) -> dict:
    episodes = list_episodes(user_id=user_id, limit=200)
    settled = [e for e in episodes if e.get('status') != 'running']
    priors = {p['domain']: p for p in list_priors(user_id=user_id)}
    domains = {}
    for domain, prior in sorted(priors.items()):
        if domain == '_global':
            continue
        domains[domain] = {
            'samples': prior['samples'],
            'failure_rate': prior['failure_rate'],
            'tokens_median': prior['tokens_median'],
        }
    factors = [h for h in list_hypotheses(user_id=user_id, status='admitted')]
    factors.sort(key=lambda h: -abs(h['evidence']['lift']))
    gaps = [g for g in list_capability_gaps(user_id=user_id)
            if g.get('status') == 'proposed']
    gaps.sort(key=lambda g: -int(g.get('samples') or 0))
    clusters = cluster_observations(user_id=user_id)
    return {
        'episodes': {'settled': len(settled),
                     'running': len(episodes) - len(settled)},
        'domains': domains,
        'admitted_factors': [
            {'statement': h['statement'], 'domain': h['domain'],
             'lift': h['evidence']['lift']} for h in factors[:_CAP]],
        'pending_hypotheses': len(list_hypotheses(
            user_id=user_id, status='proposed')),
        'capability_gaps': [
            {'summary': g['summary'], 'samples': g['samples']}
            for g in gaps[:_CAP]],
        'strategies': len(list_strategies(user_id=user_id)),
        'anti_patterns': len(list_anti_patterns(user_id=user_id)),
        'open_observations': len(list_observations(
            user_id=user_id, status='open', limit=256)),
        'observation_clusters': [
            {'cluster_id': c['cluster_id'], 'kind': c['kind'],
             'subject': c['subject'], 'count': c['count'],
             'statement': c['statement'][:160]}
            for c in clusters[:_CAP]],
        'active_mutations': [
            {'kind': m['kind'], 'version': m['version']}
            for m in list_mutations(user_id=user_id)
            if m.get('status') == 'active'][:_CAP],
        'installed_skills': [
            s['name'] for s in list_forged_skills(
                user_id=user_id, status='installed')[:_CAP]],
    }
