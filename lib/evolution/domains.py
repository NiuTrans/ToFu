"""Domain plugins: adapt existing Tofu capabilities into evolution goals.

Responsibility: first plugin — the research cycle. Entry points:
``research_goal_payload`` / ``verdict_from_research_record``. Dependencies:
none beyond the goal contract shape and the research run-record status enum
(`contracts/research_record_v1.schema.json`); the research runtime itself is
untouched — this adapter maps its outcomes, it does not rewire its engine.
``runs.py`` consumes both entry points for ``domain='research'`` runs: intake
admits the mapped goal, execution binds the research pipeline as the stage
task, and the terminal run record supplies the review verdict.

A research direction becomes an exploratory goal with a
``falsification_protocol`` criterion. Pipeline completion and model-reviewed
ideas leave scientific validity unknown, so done/degraded map to partial,
error/interrupted to failure; aborted is handled upstream. Only explicitly
version-zero legacy runs retain the old done-to-success mapping. These are
delivery receipts, never scientific proof or authority for strategy learning.
"""
from __future__ import annotations

_OUTCOME_BY_STATUS = {
    'done': 'success',
    'degraded': 'partial',
    'error': 'failure',
    'interrupted': 'failure',
}


def research_goal_payload(direction: str, *, goal_id: str,
                          budget_envelope: dict, lang: str = 'en',
                          created_at: int = 0, acceptance_version: int = 1) -> dict:
    """Map one research direction onto an admissible evolution goal."""
    _validate_acceptance_version(acceptance_version)
    return {
        'schema_version': 1,
        'goal_id': goal_id,
        'statement': f'Research direction: {direction}'[:4000],
        'domain': 'research',
        'lang': lang if lang in ('en', 'zh') else 'en',
        'exploratory': acceptance_version == 1,
        'criteria': [{
            'criterion_id': 'falsification',
            'kind': 'falsification_protocol',
            'params': {'direction': direction[:2000]},
        }],
        'budget_envelope': budget_envelope,
        'status': 'draft',
        'created_at': int(created_at or 0),
    }


def _validate_acceptance_version(version: int) -> None:
    if type(version) is not int or version not in (0, 1):
        raise ValueError('unsupported research acceptance policy version')


def verdict_from_research_record(record: dict, *, acceptance_version: int = 1) -> dict | None:
    """Settle delivery without deriving scientific acceptance from task status.

    Proposal scores, accepted-count and model assertions are deliberately not
    scientific measurements. Version zero is only for frozen legacy contracts.
    """
    _validate_acceptance_version(acceptance_version)
    status = str((record or {}).get('status') or '')
    outcome = _OUTCOME_BY_STATUS.get(status)
    if acceptance_version == 1 and status == 'done':
        outcome = 'partial'
    if outcome is None:
        return None
    task_id = str(record.get('task_id') or '')
    return {
        'outcome': outcome,
        'assessed_by': 'checker',
        'criterion_results': [{
            'criterion_id': 'falsification',
            'outcome': ('pass' if outcome == 'success'
                        else 'unknown' if outcome == 'partial' else 'fail'),
            'evidence_ref': f'research-run:{task_id}'[:500],
        }],
        'evidence_refs': [f'research-run:{task_id}'[:500]],
    }
