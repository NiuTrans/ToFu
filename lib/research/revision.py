"""Bounded critique/revision contract and checkpointed research-cycle runner.

Idea lineage is ``idea_id`` + ``idea_version``. History snapshots are deep copies;
review silence never resolves a prior issue. Only explicit, evidenced re-review
resolutions can do so. Generation remains behind the ordinary ideation gates.
"""
from __future__ import annotations

from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied

from copy import deepcopy
import hashlib
import json
import os
from typing import Literal, TypedDict

DEFAULT_MAX_REVISIONS = 2
MAX_REVISIONS = 2
MAX_ISSUES = 24
CycleStatus = Literal['reviewing', 'revising', 'complete', 'unresolved',
                      'budget_exhausted', 'no_change', 'error']


class RevisionIssue(TypedDict):
    issue_id: str
    idea_id: str
    target: str
    priority: str
    change: str
    evidence: str
    status: Literal['unresolved', 'resolved']
    resolution_evidence: str


class ResearchCycle(TypedDict):
    status: CycleStatus
    revision_count: int
    history: list[dict]
    issues: list[RevisionIssue]
    next_action: str


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()[:16]


def identify(artifact: dict) -> dict:
    out = deepcopy(artifact)
    for group in ('accepted', 'rejected'):
        for index, idea in enumerate(out.get(group) or []):
            idea['idea_id'] = str(idea.get('idea_id') or idea.get('id') or
                                  'idea_' + _digest([group, index, idea]))
            idea['idea_version'] = int(idea.get('idea_version') or 1)
    return out


def critique_issues(evaluation: dict) -> list[RevisionIssue]:
    issues = []
    rows = evaluation.get('recommended_changes') or []
    if len(rows) > MAX_ISSUES:
        raise ValueError('review exceeds bounded issue capacity')
    for row in rows:
        if not isinstance(row, dict):
            continue
        change = str(row.get('change') or '').strip()[:600]
        evidence = str(row.get('evidence') or '').strip()[:600]
        if not change:
            continue
        identity = [row.get('target'), row.get('idea_id'), row.get('gap_id'), change]
        issues.append(dict(issue_id='issue_' + _digest(identity),
                           idea_id=str(row.get('idea_id') or '')[:160],
                           target=str(row.get('target') or ''),
                           target_version=str(row.get('target_version') or ''),
                           gap_id=str(row.get('gap_id') or '')[:160],
                           priority=str(row.get('priority') or ''),
                           change=change, evidence=evidence,
                           status='unresolved', resolution_evidence=''))
    return issues


def actionable(issue: dict) -> bool:
    return (issue['status'] == 'unresolved' and issue['target'] in ('ideate', 'retrieval', 'survey')
            and issue['priority'] in ('high', 'medium') and bool(issue['evidence']))


# Scientific content is explicit: new diagnostics must not trigger paid reviews.
SCIENTIFIC_FIELDS = (
    'title', 'kind', 'linked_gap_id', 'corpus_anchor_id', 'corpus_delta',
    'failure_cause', 'new_invariant', 'intervention_level', 'core_mechanism',
    'novelty_claim', 'falsifiable_prediction', 'why_not_AB',
    'mechanism_delta', 'closest_neighbor',
)
MAX_CONTEXT_IDEAS = 12
MAX_FIELD_CHARS = 1200


def _content(artifact):
    return {row['idea_id']: {key: row[key] for key in SCIENTIFIC_FIELDS
                             if row.get(key) not in (None, '')}
            for group in ('accepted', 'rejected')
            for row in artifact.get(group, [])}


def _revision_context(current, issues, index):
    rows = [row for group in ('accepted', 'rejected')
            for row in current.get(group, [])]
    # Never silently drop or truncate a lineage to fit the prompt.
    if len(rows) > MAX_CONTEXT_IDEAS:
        raise ValueError('revision context exceeds 12 idea lineages')
    if any(len(row['idea_id']) > 160 for row in rows):
        raise ValueError('revision idea lineage exceeds 160 characters')

    def bounded(value, limit=MAX_FIELD_CHARS):
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return text[:limit]

    previous = {}
    for group in ('accepted', 'rejected'):
        previous[group] = [dict(
            idea_id=row['idea_id'], idea_version=row['idea_version'],
            **{key: bounded(row[key], 240 if key == 'title' else MAX_FIELD_CHARS)
               for key in SCIENTIFIC_FIELDS if row.get(key) not in (None, '')})
            for row in current.get(group, [])]
    return {'revision_number': index, 'previous_ideas': previous,
            'issues': [{key: bounded(issue.get(key, ''), limit)
                        for key, limit in (
                            ('issue_id', 160), ('idea_id', 160), ('target', 32),
                            ('priority', 16), ('change', 600), ('evidence', 600),
                            ('status', 16), ('resolution_evidence', 600))}
                       for issue in issues if actionable(issue)][:MAX_ISSUES]}


def run_revision_cycle(original, *, review, revise, gate, abort=None,
                       workdir=None, identity=None, max_revisions=DEFAULT_MAX_REVISIONS,
                       survey=None, harvest=None, retrieve=None, rebuild=None):
    """Return final ideas, raw latest evaluation, and bounded audit state.

    Each paid phase has its own production checkpoint. The directory is keyed
    by frozen inputs, preventing stale nested checkpoints after upstream edits.
    """
    from lib.production.stages import Stage, StageAborted, run_stages
    from lib.research.telemetry import aggregate_research_usage

    limit = max(0, min(MAX_REVISIONS, int(max_revisions)))
    initial = dict(current=identify(original), evaluation={},
                   research_cycle=dict(status='reviewing', revision_count=0,
                                       history=[], issues=[], next_action='review'),
                   revision_usage={}, review_usage={},
                   generation_budget_exhausted=bool(
                       original.get('degraded') or
                       (original.get('usage') or {}).get('forced_final')))
    initial['research_cycle']['generation_budget_exhausted'] = initial['generation_budget_exhausted']
    initial.update(survey=deepcopy(survey), harvest=deepcopy(harvest),
                   survey_revision_usage={}, retrieval_usage={})

    def check_abort():
        if abort and abort():
            raise StageAborted('research revision aborted')

    def stop(state, status):
        state['research_cycle']['status'] = status
        state['research_cycle']['next_action'] = (
            'none' if status == 'complete' else 'inspect_unresolved_issues')
        return state

    def step(previous, phase, index):
        state = deepcopy(previous)
        cycle = state['research_cycle']
        if cycle['status'] not in ('reviewing', 'revising'):
            return state
        check_abort()
        try:
            upstream = any(actionable(i) and i['target'] in ('retrieval', 'survey')
                           for i in cycle['issues'])
            if phase == 'retrieve':
                if not upstream:
                    return state
                if not rebuild or not state.get('survey'):
                    cycle['unverified_reason'] = 'survey repair unavailable'
                    return stop(state, 'unresolved')
                if any(actionable(i) and i['target'] == 'retrieval' for i in cycle['issues']):
                    if not retrieve:
                        cycle['unverified_reason'] = 'targeted retrieval unavailable'
                        return stop(state, 'unresolved')
                    result = retrieve(deepcopy(state), index)
                    state['retrieval_usage'] = aggregate_research_usage(
                        state['retrieval_usage'], result.get('usage') or {})['total']
                    if not result.get('ok'):
                        cycle['unverified_reason'] = result.get('error') or 'retrieval unverified'
                        return stop(state, 'unresolved')
                    state['harvest'] = result['harvest']
                    if (result.get('usage') or {}).get('forced_final'):
                        return stop(state, 'budget_exhausted')
                return state
            elif phase == 'survey':
                if not upstream:
                    return state
                result = rebuild(deepcopy(state), index)
                state['survey_revision_usage'] = aggregate_research_usage(
                    state['survey_revision_usage'], result.get('usage') or {})['total']
                if not result.get('ok'):
                    cycle['unverified_reason'] = result.get('error') or 'survey unverified'
                    return stop(state, 'unresolved')
                state['survey'] = result['survey']
                state['survey']['survey_version'] = index + 1
                if (result.get('usage') or {}).get('forced_final'):
                    return stop(state, 'budget_exhausted')
                # Absence is not proof of closure. Only an explicitly closed gap
                # retires a lineage; every other lineage is revalidated by ideation.
                gm = state['survey'].get('open_gaps') or {}
                closed = {r.get('id') for r in gm.get('closed_gaps', []) if isinstance(r, dict)}
                closed.update(r.get('id') for r in gm.get('open_gaps', [])
                              if isinstance(r, dict) and r.get('status') == 'closed')
                closed = {gap_id for gap_id in closed
                          if isinstance(gap_id, str) and gap_id.strip()}
                retired = []
                for group in ('accepted', 'rejected'):
                    keep = []
                    for idea in state['current'].get(group, []):
                        if idea.get('linked_gap_id') in closed:
                            retired.append({**idea, 'retired': True,
                                            'idea_version': idea['idea_version'] + 1,
                                            'reject_stage': 'survey_revision',
                                            'reject_reason': 'linked gap explicitly closed by revised evidence'})
                        else:
                            keep.append(idea)
                    state['current'][group] = keep
                state['current'].setdefault('retired', []).extend(retired)
                cycle['dependencies_revalidated'] = 'all candidate gap links and novelty claims'
                return state
            elif phase == 'revise':
                cycle['revision_count'] += 1
                context = _revision_context(state['current'], cycle['issues'], index)
                if upstream:
                    context['current_survey'] = state['survey']
                    context['revalidate_all_dependencies'] = True
                if not any(state['current'].get(g) for g in ('accepted', 'rejected')):
                    cycle['status'] = 'reviewing'
                    return state
                candidate = revise(context)
                check_abort()
                state['revision_usage'] = aggregate_research_usage(
                    state['revision_usage'], candidate.get('usage') or {})['total']
                if not candidate.get('ok') or gate(candidate):
                    raise ValueError(candidate.get('error') or 'revision gate did not run')
                candidate = identify(candidate)
                old = {i['idea_id']: i for g in ('accepted', 'rejected')
                       for i in state['current'].get(g, [])}
                for g in ('accepted', 'rejected'):
                    for idea in candidate.get(g, []):
                        if idea['idea_id'] not in old:
                            raise ValueError('revision returned unknown idea lineage')
                        idea['idea_version'] = old[idea['idea_id']]['idea_version'] + 1
                ids = [i['idea_id'] for g in ('accepted', 'rejected')
                       for i in candidate.get(g, [])]
                if len(ids) != len(set(ids)) or set(ids) != set(old):
                    raise ValueError('revision must preserve every idea lineage')
                if not upstream and _content(candidate) == _content(state['current']):
                    return stop(state, 'no_change')
                candidate['retired'] = deepcopy(state['current'].get('retired') or [])
                state['current'] = candidate
                state['generation_budget_exhausted'] = bool(
                    candidate.get('degraded') or
                    (candidate.get('usage') or {}).get('forced_final'))
                cycle['generation_budget_exhausted'] = state['generation_budget_exhausted']
                cycle['status'] = 'reviewing'
            else:
                args = (state['current'], cycle['issues'])
                if survey is not None:
                    args += (state['survey'], state['harvest'])
                result = review(*args)
                check_abort()
                state['evaluation'] = result
                state['review_usage'] = aggregate_research_usage(
                    state['review_usage'], result.get('usage') or {})['total']
                cycle['history'].append({'revision': index,
                                         'ideas': deepcopy(state['current']),
                                         'survey': deepcopy(state['survey']),
                                         'harvest': deepcopy(state['harvest']),
                                         'evaluation': deepcopy(result)})
                if (result.get('usage') or {}).get('forced_final'):
                    return stop(state, 'budget_exhausted')
                if not result.get('ok') or result.get('degraded'):
                    return stop(state, 'error')
                known = {i['issue_id']: i for i in cycle['issues']}
                for resolution in result.get('issue_resolutions') or []:
                    issue = known.get(resolution.get('issue_id'))
                    if (issue and resolution.get('resolved') is True
                            and str(resolution.get('evidence') or '').strip()):
                        issue['status'] = 'resolved'
                        issue['resolution_evidence'] = str(resolution['evidence'])[:600]
                for issue in critique_issues(result):
                    issue['target_version'] = issue.get('target_version') or str(index + 1)
                    known[issue['issue_id']] = issue
                if len(known) > MAX_ISSUES:
                    raise ValueError('review exceeds bounded issue capacity')
                cycle['issues'] = list(known.values())
                if not any(i['status'] == 'unresolved' for i in cycle['issues']):
                    return stop(state, 'complete')
                if not any(actionable(i) for i in cycle['issues']):
                    return stop(state, 'unresolved')
                if index >= limit or state.get('generation_budget_exhausted'):
                    return stop(state, 'budget_exhausted')
                cycle['status'] = 'revising'
                cycle['next_action'] = 'repair_evidence_and_revalidate' if any(
                    actionable(i) and i['target'] in ('retrieval', 'survey')
                    for i in cycle['issues']) else 'revise_ideas'
        except ModelBudgetAdmissionDenied:
            raise
        except StageAborted:
            raise
        except Exception as exc:
            check_abort()
            cycle['error'] = f'{type(exc).__name__}: {exc}'[:600]
            return stop(state, 'error')
        return state

    stages = []
    for phase, index in [('review', 0)] + [(p, i) for i in range(1, limit + 1)
                                          for p in ('retrieve', 'survey', 'revise', 'review')]:
        previous_name = stages[-1].name if stages else None
        def run(ctx, phase=phase, index=index, previous_name=previous_name):
            previous = ctx['artifacts'][previous_name] if previous_name else initial
            return step(previous, phase, index)
        stages.append(Stage(f'{phase}_{index}', run, checkpoint_version='revision-v4'))
    if workdir:
        path = os.path.join(workdir, 'revision_' + _digest([original, identity, limit, survey, harvest]) + '.json')
        artifacts = run_stages(stages, {}, state_path=path, abort_check=abort)
        return artifacts[stages[-1].name]
    ctx = {'artifacts': {}}
    for stage in stages:
        ctx['artifacts'][stage.name] = stage.run(ctx)
    return ctx['artifacts'][stages[-1].name]
