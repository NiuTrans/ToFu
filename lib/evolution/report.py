"""Experiment reports (north star): the episode ledger as an evidence-backed paper draft.

Responsibility: render one goal's full trajectory — plan, estimates versus
actuals, verdicts with evidence, strategies applied, cost analysis, open
limitations, next steps — into a structured Markdown report. Entry point:
``render_goal_report``. Dependencies: the goals/plans/episodes stores, the
self-model, capability gaps, strategies.

Every number in the report traces to a receipt: estimates quote the plan,
actuals quote the episode, verdicts quote their evidence references. Where
no data exists the section says so — the report never fills a gap with
fluent prose. This is the deterministic spine; LLM polish is a later
meta-goal whose input is exactly this document.
"""
from __future__ import annotations

from .episodes import list_episodes
from .gate import list_capability_gaps
from .goals import get_goal, get_plan
from .self_model import list_hypotheses, predict_success
from .strategies import retrieve_strategies

_OUTCOME = {'success': 'Success', 'partial': 'Partial', 'failure': 'Failure',
            'aborted': 'Aborted'}


def _calibration_section(episodes: list[dict], plans: dict) -> list[str]:
    lines = ['## Results: estimated vs actual', '']
    rows = []
    for episode in episodes:
        plan = plans.get(episode.get('plan_id')) or {}
        candidate = next((c for c in plan.get('candidates', [])
                          if c.get('path_id') == episode.get(
                              'selected_path_id')), None)
        estimate = (candidate or {}).get('cost_estimate') or {}
        actual = episode.get('actuals') or {}
        qualifier = ' (observed lower bound; consumption incomplete)' if episode.get('actuals_complete') is False else ''
        rows.append(
            f"| {episode['episode_id'][:12]} "
            f"| {estimate.get('expected_tokens', '—')} "
            f"| {actual.get('tokens', '—')}{qualifier} "
            f"| {estimate.get('expected_tool_calls', '—')} "
            f"| {actual.get('tool_calls', '—')} "
            f"| {_OUTCOME.get(episode['status'], episode['status'])} |")
    if not rows:
        return lines + ['No settled episodes yet.', '']
    lines += ['| episode | est. tokens | tokens | est. calls | calls | outcome |',
              '|---|---|---|---|---|---|'] + rows + ['']
    return lines


def render_goal_report(goal_id: str, *, user_id) -> str:
    goal = get_goal(goal_id, user_id=user_id)
    if goal is None:
        return ''
    domain = str(goal.get('domain') or 'general')
    episodes = [e for e in list_episodes(user_id=user_id, limit=200)
                if e.get('goal_id') == goal_id]
    episodes.sort(key=lambda e: int(e.get('settled_at') or 0))
    plans = {}
    for episode in episodes:
        plan_id = episode.get('plan_id') or ''
        if plan_id not in plans:
            plans[plan_id] = get_plan(plan_id, user_id=user_id) or {}

    lines = [f"# Experiment report: {goal['statement'][:200]}", '']
    lines += ['## Objective', '',
              f"- domain: `{domain}`",
              f"- exploratory: {'yes' if goal.get('exploratory') else 'no'}",
              '- criteria: ' + (', '.join(
                  f"{c['criterion_id']} ({c['kind']})"
                  for c in goal.get('criteria') or []) or 'none declared'),
              f"- budget envelope: {goal.get('budget_envelope')}", '']

    verdicts = [e for e in episodes if e.get('verdict')]
    lines += ['## Verdicts', '']
    if verdicts:
        for episode in verdicts:
            verdict = episode['verdict']
            evidence = ', '.join(verdict.get('evidence_refs') or
                                 [r.get('evidence_ref') or ''
                                  for r in verdict.get('criterion_results',
                                                       [])]) or 'no refs'
            lines.append(f"- `{episode['episode_id'][:12]}` "
                         f"**{verdict['outcome']}** "
                         f"(by {verdict['assessed_by']}); evidence: {evidence}")
    else:
        lines.append('No verdicts recorded yet.')
    lines.append('')

    lines += _calibration_section(episodes, plans)

    strategies = retrieve_strategies(str(goal.get('statement') or ''),
                                     user_id=user_id, domain=domain, limit=3)
    lines += ['## Methods observed to work here', '']
    if strategies:
        for template in strategies:
            stages = ' → '.join(str(s.get('kind') or '')
                                for s in template.get('stages', []))
            lines.append(f"- `{stages}` — {template['success_rate']:.0%} "
                         f"over {template['samples']} episodes")
    else:
        lines.append('No strategy templates match this goal yet.')
    factors = [h for h in list_hypotheses(user_id=user_id, status='admitted')
               if h['domain'] in ('*', domain)]
    if factors:
        lines += ['', '## Self-model factors (validated on data)', '']
        for factor in factors:
            lines.append(f"- {factor['statement']} "
                         f"(lift {factor['evidence']['lift']:+.2f}, "
                         f"n={factor['evidence']['samples_match']}"
                         f"/{factor['evidence']['samples_rest']})")
    lines.append('')

    gaps = [g for g in list_capability_gaps(user_id=user_id)
            if g.get('status') == 'proposed']
    lines += ['## Limitations and open gaps', '']
    if gaps:
        for gap in gaps[:6]:
            lines.append(f"- {gap['summary']} (seen ×{gap['samples']})")
    else:
        lines.append('No open capability gaps recorded.')
    prediction = predict_success(domain, {}, user_id=user_id)
    lines += ['', '## Next steps', '']
    if prediction:
        lines.append(f"- self-model baseline success estimate for `{domain}`: "
                     f"{prediction['baseline']:.0%}; factors applied: "
                     f"{len(prediction['factors_applied'])}")
    lines.append('- unresolved probes and gaps above feed the curriculum; '
                 'each next attempt should cite which gap it addresses.')
    lines.append('')
    return '\n'.join(lines)
