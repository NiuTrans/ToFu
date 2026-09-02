"""Pure scoring and replay helpers for the Tool Search benchmark."""

from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Any, Callable


SearchFn = Callable[..., dict[str, Any]]


def merge_simulated_users(cases: list[dict[str, Any]], payload: Any
                          ) -> list[dict[str, Any]]:
    """Merge validated LLM paraphrases without allowing oracle mutation."""
    by_id = {str(case['id']): case for case in cases}
    rows = payload.get('cases') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return [dict(case) for case in cases]
    additions: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get('id') or '') not in by_id:
            continue
        utterances = row.get('utterances')
        if not isinstance(utterances, list):
            continue
        additions[str(row['id'])] = [
            str(value).strip()[:500] for value in utterances
            if isinstance(value, str) and value.strip()
        ][:8]
    out = []
    for case in cases:
        item = dict(case)
        item['utterances'] = list(dict.fromkeys([
            *case.get('seeds', []), *additions.get(str(case['id']), []),
        ]))
        out.append(item)
    return out


def flatten_episodes(cases: list[dict[str, Any]]) -> list[dict[str, str]]:
    episodes = []
    for case in cases:
        utterances = case.get('utterances') or case.get('seeds') or []
        for index, utterance in enumerate(utterances):
            episodes.append({
                'episode_id': f'{case["id"]}:{index}',
                'case_id': str(case['id']),
                'target': str(case['target']),
                'utterance': str(utterance),
                'query': str(utterance),
            })
    return episodes


def apply_model_queries(episodes: list[dict[str, str]], payload: Any
                        ) -> list[dict[str, str]]:
    """Apply model-generated search/direct decisions by episode id."""
    decisions = payload.get('decisions') if isinstance(payload, dict) else None
    by_id = {}
    if isinstance(decisions, list):
        by_id = {
            str(row.get('episode_id')): row for row in decisions
            if isinstance(row, dict) and row.get('episode_id')
        }
    out = []
    for episode in episodes:
        item = dict(episode)
        row = by_id.get(item['episode_id']) or {}
        action = str(row.get('action') or 'search').strip().lower()
        if action == 'direct' and str(row.get('name') or '').strip():
            item['action'] = 'direct'
            item['direct_name'] = str(row['name']).strip()
        else:
            item['action'] = 'search'
            item['query'] = (str(row.get('query') or '').strip()
                             or item['utterance'])[:500]
        out.append(item)
    return out


# Compatibility for frozen reports created before the terminology was fixed.
apply_agent_decisions = apply_model_queries


def evaluate_retrieval(
    catalog: list[dict[str, Any]],
    episodes: list[dict[str, str]],
    *,
    search: SearchFn,
    limit: int = 5,
    namespace_by_name: dict[str, str] | None = None,
    search_text_by_name: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Score exact ground truth; LLMs may propose queries, never verdicts."""
    rows = []
    ranks = []
    direct = 0
    direct_correct = 0
    for episode in episodes:
        if episode.get('action') == 'direct':
            direct += 1
            correct = episode.get('direct_name') == episode['target']
            direct_correct += int(correct)
            rows.append({**episode, 'rank': 1 if correct else None,
                         'matches': [], 'correct': correct})
            ranks.append(1 if correct else None)
            continue
        kwargs = {
            'limit': limit,
            'namespace_by_name': namespace_by_name or {},
        }
        if search_text_by_name is not None:
            kwargs['search_text_by_name'] = search_text_by_name
        result = search(catalog, episode.get('query') or '', **kwargs)
        matches = [str(item.get('name') or '')
                   for item in result.get('items') or []
                   if isinstance(item, dict)]
        rank = matches.index(episode['target']) + 1 \
            if episode['target'] in matches else None
        ranks.append(rank)
        rows.append({
            **episode, 'rank': rank, 'matches': matches,
            'correct': rank == 1,
        })
    total = len(rows)
    misses = Counter(row['case_id'] for row in rows if row['rank'] is None)
    return {
        'episodes': total,
        'recall_at_1': round(sum(rank == 1 for rank in ranks) / total, 4)
        if total else 0.0,
        'recall_at_5': round(sum(rank is not None and rank <= 5
                                 for rank in ranks) / total, 4)
        if total else 0.0,
        'mrr': round(mean((1 / rank) if rank else 0 for rank in ranks), 4)
        if total else 0.0,
        'empty_result_rate': round(sum(not row['matches']
                                       and row.get('action') != 'direct'
                                       for row in rows) / total, 4)
        if total else 0.0,
        'direct_calls': direct,
        'direct_accuracy': round(direct_correct / direct, 4) if direct else None,
        'misses_by_case': dict(sorted(misses.items())),
        'rows': rows,
    }


def apply_tool_selections(report: dict[str, Any], payload: Any
                          ) -> dict[str, Any]:
    """Attach model selections and score them against frozen ground truth."""
    raw = payload.get('selections') if isinstance(payload, dict) else None
    selections = {
        str(row.get('episode_id')): str(row.get('tool_name') or '').strip()
        for row in (raw or []) if isinstance(row, dict) and row.get('episode_id')
    }
    rows = []
    for row in report.get('rows') or []:
        item = dict(row)
        selected = selections.get(str(item.get('episode_id') or ''), '')
        # A direct action already selected its tool on the first step.
        if item.get('action') == 'direct' and not selected:
            selected = str(item.get('direct_name') or '')
        item['selected_tool'] = selected
        item['selection_correct'] = selected == item.get('target')
        rows.append(item)
    total = len(rows)
    report = dict(report)
    report['rows'] = rows
    report['end_to_end_accuracy'] = round(
        sum(row['selection_correct'] for row in rows) / total, 4
    ) if total else 0.0
    report['selection_missing_rate'] = round(
        sum(not row['selected_tool'] for row in rows) / total, 4
    ) if total else 0.0
    return report


def v2_release_gate(report: dict[str, Any], *, unauthorized_executions: int,
                    end_to_end_accuracy: float | None = None) -> dict[str, Any]:
    """Apply the pre-registered Tool Search v2 release thresholds."""
    recall = float(report.get('recall_at_5') or 0)
    selection = (float(end_to_end_accuracy)
                 if end_to_end_accuracy is not None else
                 float(report.get('end_to_end_accuracy') or 0))
    gates = {
        'recallAt5AtLeast99Percent': recall >= 0.99,
        'endToEndSelectionAtLeast97Percent': selection >= 0.97,
        'zeroUnauthorizedExecutions': int(unauthorized_executions) == 0,
    }
    return {'gates': gates, 'releaseEligible': all(gates.values())}


__all__ = [
    'apply_agent_decisions', 'apply_model_queries', 'apply_tool_selections',
    'evaluate_retrieval', 'flatten_episodes', 'merge_simulated_users',
    'v2_release_gate',
]
