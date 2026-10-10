"""Flow-template crystallization: recurring winning sequences become saved
orchestration definitions through the gate.

Responsibility: synthesize the small validated definition a ``flow_template``
mutation carries and materialize it into the owner's saved definition
catalogue when the mutation activates. Entry points: ``build_flow_definition``
(deterministic synthesis) and ``materialize_flow_template`` (activation side
effect). Dependencies: the orchestration definition contract owns validation;
the definition application service owns persistence; the gate owns the
decision. A rolled-back template stays in the catalogue as an inert saved
definition — crystallized knowledge remains owner-usable and is deleted only
through the ordinary Studio surface.
"""
from __future__ import annotations

from .contracts import EvolutionStateError

MAX_STEPS_GUIDANCE = 600


def build_flow_definition(sequence, *, title: str) -> dict:
    """Worker → critic → deliverable graph for one recurring winning sequence.

    The sequence rides in the worker objective as evidence-derived guidance,
    never as a hidden tool allowlist — the role runtime keeps tool authority.
    """
    steps = ' → '.join(sequence)[:MAX_STEPS_GUIDANCE]
    return {
        'schema': 'tofu.orchestration/v1',
        'name': f'结晶流程: {title}'[:120],
        'nodes': [
            {'id': 's', 'type': 'control', 'kind': 'start'},
            {'id': 'w', 'type': 'role', 'role': 'worker',
             'params': {'objective': (
                 f'完成用户目标。已验证的高效工具路径（{len(sequence)} 步）: '
                 f'{steps}——优先沿此路径推进，证据不足时自由调整。')}},
            {'id': 'c', 'type': 'role', 'role': 'critic',
             'params': {'objective': (
                 '对照原始目标验收 worker 的产出：证据是否充分、结论是否'
                 '可复现；不充分则给出具体返工意见。')}},
            {'id': 'a', 'type': 'control', 'kind': 'artifact',
             'params': {'path': 'deliverable.md'}},
            {'id': 'z', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 's', 'to': 'w'},
            {'from': 'w', 'to': 'c'},
            {'from': 'c', 'to': 'a'},
            {'from': 'a', 'to': 'z'},
        ],
    }


def validate_flow_payload(payload: dict) -> None:
    """Structural sandbox for the gate: the carried definition must pass the
    orchestration contract with zero errors."""
    definition = (payload or {}).get('definition')
    if not isinstance(definition, dict):
        raise EvolutionStateError(
            'flow_template payload.definition must be an object')
    from lib.orchestration._validate import validate_definition
    verdict = validate_definition(definition)
    errors = verdict.get('errors') or []
    if errors:
        raise EvolutionStateError(
            f'flow_template definition invalid: {errors[:2]}')


def materialize_flow_template(record: dict, *, user_id,
                              definition_creator=None) -> None:
    """Create the saved definition an activated ``flow_template`` carries.

    Runs before the gate's active transition commits; a refused create raises
    so the mutation stays ``confirmed`` and retryable instead of activating
    without its artifact.
    """
    validate_flow_payload(record.get('payload') or {})
    create = definition_creator or _default_definition_creator
    result = create((record['payload'])['definition'], user_id)
    if getattr(result, 'entry', None) is None:
        raise EvolutionStateError(
            'flow_template definition was not accepted by the catalogue')


def _default_definition_creator(definition, user_id):
    from lib.orchestration.definition_service import (
        OrchestrationDefinitionService,
    )
    return OrchestrationDefinitionService.for_owner(user_id).create(definition)
