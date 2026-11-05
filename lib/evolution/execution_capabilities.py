"""Adapt explicit workflow requirements to shared skills/research owners.

Entry points bind_selection, validate_research_context and requirements.
No catalog, model loop, transport, storage or tool permission policy lives here.
"""
from .contracts import EvolutionContractError


def bind_selection(payload, *, user_id):
    selection = payload.get('skill_selection') or []
    expected = payload.get('skill_bindings')
    if expected is not None and [row['skill_id'] for row in expected] != selection:
        raise EvolutionContractError('skill bindings do not match the selected skill IDs')
    if not selection:
        return []
    from lib.skills.activation import bind_skill_selection
    try:
        return bind_skill_selection(selection, project_path=payload.get('project_path'),
                                    owner_user_id=user_id, expected=expected)
    except ValueError as exc:
        raise EvolutionContractError(str(exc)) from exc


def validate_research_context(context, *, user_id):
    bindings = (context or {}).get('capability_bindings') or []
    if not any(row.get('enabled') for row in bindings):
        return
    from lib.research.capabilities import build_capability_catalog, validate_bindings
    checked = validate_bindings(bindings, catalog=build_capability_catalog(user_id=user_id))
    if checked['problems']:
        issues = '; '.join(f"{row['capability']}: {row['code']}" for row in checked['problems'])
        raise EvolutionContractError(f'Selected research capabilities are unavailable: {issues}')


def requirements(run):
    checks = list(run.get('acceptance_requirements') or [])
    for binding in run.get('skill_bindings') or []:
        checks.append(f"Apply skill {binding['skill_id']} (guide {binding['content_hash']}) to this stage. "
                      'Report each applicable check with actual evidence and explain inapplicable checks. '
                      'Loading the guide or asserting compliance is not completion evidence.')
    if run.get('research_context'):
        checks.append('Respect the owner-selected research hypothesis, protocol and capability bindings. '
                      'Retain falsifiers, baseline, evaluation and stop conditions; report unmet constraints '
                      'with evidence. These inputs do not establish scientific validity or authorize publication.')
    return {f'requirement-{index + 1}': check for index, check in enumerate(checks)}


def research_context_prompt(run):
    if not run.get('research_context'):
        return ''
    import json
    return ('\nOwner-selected research constraints (inputs, not observed results):\n'
            + json.dumps(run['research_context'], ensure_ascii=False)
            + '\nUse the selected exact tools and argument defaults where applicable. '
            'Bindings do not grant external publication or bypass current tool approval policy.\n')
