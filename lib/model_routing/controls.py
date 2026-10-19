"""Read-only composer controls derived from one owner routing revision.

Uses the runtime candidate compiler and inference resolver; no browser-side
provider policy, probes, credentials, mutable cache, or independent catalogue.
Auto-provider controls are the intersection of all eligible deployment choices
so selecting a preset cannot silently change meaning after provider failover.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from .domain import ModelRef, NativeModelSelection, ProviderOfferingRef
from .routing import RouteCandidateCompiler, RoutePolicy

from .operations import CHAT_PROTOCOLS



def _choices(profile: Mapping) -> tuple[str, bool | None, tuple[str, ...]]:
    reasoning = profile.get('reasoning') or {}
    support = reasoning.get('support', 'unknown')
    disable = reasoning.get('can_disable')
    choices = ['default']
    if support == 'unsupported':
        return support, True, ('default', 'off')
    if support != 'supported' or not reasoning.get('wire_format'):
        return 'unknown', None, tuple(choices)
    from lib.llm.reasoning import ReasoningContractError, resolve_reasoning_control

    # Declared rungs are the UI authority. Compatibility aliases (medium→high,
    # ultra→max, lowest→first rung) remain accepted for old callers but must
    # not manufacture extra slider positions or hide future effort names.
    efforts = reasoning.get('supported_efforts')
    disabled = reasoning.get('disabled_effort', 'none')
    depths = (['off'] + [e for e in efforts if e not in ('none', disabled)]
              if efforts else ['off', 'enabled'])
    for depth in depths:
        if depth == 'off' and disable is not True:
            continue
        mode = depth if depth in ('off', 'lowest', 'enabled') else 'preset'
        try:
            _, enabled, resolved, out_mode = resolve_reasoning_control(
                reasoning, mode, depth)
        except ReasoningContractError:
            continue
        # The resolver degrades rather than rejects product intents, so the
        # composer must hide a rung the destination cannot actually express:
        # an off that fell back to a thinking rung, a preset that clamped to a
        # different effort, or an enabled rung with no effort to select.
        if mode == 'off' and (out_mode != 'off' or enabled):
            continue
        if mode == 'preset' and resolved != depth:
            continue
        if mode == 'enabled' and enabled is not True:
            continue
        if depth not in choices:
            choices.append(depth)
    # A declared ability without an installed wire binding is not executable.
    if disable is True and 'off' not in choices:
        disable = None
    return support, disable, tuple(choices)


def reasoning_controls(document: Mapping) -> list[dict]:
    """Return secret-free controls for each model/provider preference.

    One build-local compiler indexes the aggregate. Each deployment is visited
    only once despite multiple credential grants; no secret is resolved.
    """
    from .inference import candidate_inference_metadata, resolve_inference_profile
    from .offering_metadata import apply_inherited_model_fields
    from lib.llm.reasoning import reasoning_depth_restoration

    compiler = RouteCandidateCompiler(document)
    groups: dict[tuple[str, str, str, str], list[tuple]] = defaultdict(list)
    accesses = {row['provider_access_id']: row for row in document['provider_accesses']}
    models = {(m['creator_id'], m['model_id']): m for m in document['models']}
    selections = [NativeModelSelection(ModelRef(row['creator_id'], row['model_id']), None)
                  for row in document['models']]
    for offering in document['offerings']:
        if offering['identity_state'] == 'pending_identity':
            provider = accesses[offering['provider_access_id']]['provider_id']
            selections.append(NativeModelSelection(
                None, ProviderOfferingRef(provider, offering['offering_id']), provider))
    cache = {}
    for selection in selections:
        for candidate in compiler.compile_deployments(
                selection, policy=RoutePolicy(required_protocols=CHAT_PROTOCOLS)):
            deployment_id = candidate.deployment['deployment_id']
            if deployment_id not in cache:
                profile = candidate_inference_metadata(candidate)['inference_profile']
                cache[deployment_id] = (*_choices(profile),
                    profile.get('reasoning', {}).get('default_effort'))
            state = cache[deployment_id]
            if candidate.model is None:
                model_id = candidate.offering['pending_model_id']
                creator_id = ''
                offering_id = candidate.offering['offering_id']
            else:
                model_id = candidate.model['model_id']
                creator_id = candidate.model['creator_id']
                offering_id = ''
                groups[(creator_id, model_id, '', '')].append(state)
                # Saved provider/offering selections remain valid after
                # catalog identity reconciliation. Preserve their controls.
                groups[(creator_id, model_id, candidate.provider_id,
                        candidate.offering['offering_id'])].append(state)
            groups[(creator_id, model_id, candidate.provider_id, offering_id)].append(state)
    result = []
    # Execution eligibility and declared model ability are different facts.
    # Missing/revoked grants must not erase an installed model's known controls.
    # Fallback rows are display-only and never enter the candidate compiler.
    unavailable = {}
    for offering in document['offerings']:
        if not offering.get('enabled', True):
            continue
        ref = offering.get('model') or {}
        creator, model = ref.get('creator_id', ''), ref.get('model_id', offering.get('pending_model_id', ''))
        visible_metadata = dict(offering)
        apply_inherited_model_fields(visible_metadata, models.get((creator, model)))
        if 'text' not in visible_metadata.get('capabilities', []):
            continue
        provider = accesses[offering['provider_access_id']]['provider_id']
        keys = [(creator, model, provider, offering['offering_id'])]
        if creator:
            keys.extend([(creator, model, provider, ''), (creator, model, '', '')])
        keys = [key for key in keys if key not in groups]
        if not keys:
            continue
        profile = resolve_inference_profile(model=models.get((creator, model)), offering=offering)
        state = (*_choices(profile), profile.get('reasoning', {}).get('default_effort'))
        for key in keys:
            unavailable.setdefault(key, []).append(state)
    groups.update(unavailable)
    for (creator, model, provider, offering), states in sorted(groups.items()):
        supports = {state[0] for state in states}
        support = supports.pop() if len(supports) == 1 else 'unknown'
        disable_values = {state[1] for state in states}
        disable = disable_values.pop() if len(disable_values) == 1 else None
        common = set.intersection(*(set(state[2]) for state in states))
        defaults = {state[3] for state in states}
        default_depth = defaults.pop() if len(defaults) == 1 else None
        depths = [depth for depth in states[0][2] if depth in common]
        available = (creator, model, provider, offering) not in unavailable
        result.append({'creator_id': creator, 'model_id': model,
                       'provider_id': provider, 'offering_id': offering,
                       'support': support, 'can_disable': disable,
                       'availability': 'available' if available else 'no_route',
                       'default_depth': default_depth,
                       'depths': depths,
                       **({'restoration': reasoning_depth_restoration(depths)}
                          if available and support == 'supported' and len(depths) > 1 else {})})
    return result
