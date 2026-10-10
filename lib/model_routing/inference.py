"""Declared inference capabilities: validation, metadata import and route resolution.

The v2 JSON schema owns the bounded vocabulary. No model-name or endpoint
heuristics belong here; transport plugins are checked only when used.
"""
from __future__ import annotations

import copy
import math
from functools import lru_cache
from typing import Mapping


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    # Generated from the canonical schema and shipped with the Python package;
    # runtime validation must not depend on a source checkout's contracts path.
    from lib.api_v1_model_routing_generated import EXTERNAL_DEFS
    return Draft202012Validator({'$ref': '#/$defs/inferenceProfile', '$defs': EXTERNAL_DEFS})


def validate_inference_profile(value, *, field='inference_profile') -> dict:
    """Validate without coercion, then detach; reject inconsistent numeric bounds."""
    from .domain import ModelRoutingError
    error = next(_validator().iter_errors(value), None)
    if error is not None:
        # jsonschema's message includes invalid values. A profile is inspectable
        # routing metadata, never a channel for reflecting secrets or payloads.
        raise ModelRoutingError(f'{field}: invalid inference profile', field=field)
    def check_finite(node):
        if isinstance(node, float) and not math.isfinite(node):
            raise ModelRoutingError(f'{field} numbers must be finite', field=field)
        if isinstance(node, dict):
            for child in node.values():
                check_finite(child)
        elif isinstance(node, list):
            for child in node:
                check_finite(child)
    check_finite(value)
    result = copy.deepcopy(value)
    for section, low, high, default in (
        ('reasoning', 'min_budget_tokens', 'max_budget_tokens', 'default_budget_tokens'),
        ('sampling', 'min_temperature', 'max_temperature', 'temperature'),
    ):
        row = result.get(section, {})
        for key, number in row.items():
            if isinstance(number, float) and not math.isfinite(number):
                raise ModelRoutingError(f'{field}.{section}.{key} must be finite', field=field)
        if low in row and high in row and row[low] > row[high]:
            raise ModelRoutingError(f'{field}.{section} bounds are reversed', field=field)
        for key in ([default] if section == 'reasoning' else [default, 'enabled_temperature', 'disabled_temperature']):
            if isinstance(row.get(key), (int, float)) and ((low in row and row[key] < row[low]) or (high in row and row[key] > row[high])):
                raise ModelRoutingError(f'{field}.{section}.{key} exceeds bounds', field=field)
    reasoning = result.get('reasoning', {})
    if 'supported_efforts' in reasoning and 'default_effort' in reasoning and reasoning['default_effort'] not in reasoning['supported_efforts']:
        raise ModelRoutingError(f'{field}.reasoning.default_effort is not supported', field=field)
    efforts = reasoning.get('supported_efforts')
    if efforts is not None:
        if 'disabled_effort' in reasoning and reasoning['disabled_effort'] not in efforts:
            raise ModelRoutingError(f'{field}.reasoning.disabled_effort is not supported', field=field)
        if any(effort not in efforts for effort in reasoning.get('effort_map', {}).values()):
            raise ModelRoutingError(f'{field}.reasoning.effort_map target is not supported', field=field)
        if efforts == [] and reasoning.get('support') == 'supported' and reasoning.get('wire_format') == 'reasoning_effort':
            raise ModelRoutingError(f'{field}.reasoning effort wire requires a nonempty ladder', field=field)
    return result


def profile_from_metadata(row, *, source='configured') -> dict:
    """Import explicit profiles and declared catalog efforts, never name guesses."""
    if not isinstance(row, Mapping):
        return {}
    result = {}
    levels = row.get('supported_reasoning_levels')
    reasoning = {}
    if isinstance(levels, list):
        efforts = [item.get('effort') if isinstance(item, Mapping) else item for item in levels]
        efforts = list(dict.fromkeys(item for item in efforts if isinstance(item, str) and item))
        if efforts:
            reasoning['supported_efforts'] = efforts
            reasoning['support'] = 'supported'
    if row.get('default_reasoning_level'):
        reasoning['default_effort'] = row['default_reasoning_level']
    if reasoning:
        result['reasoning'] = reasoning
    wire_format = row.get('thinking_format')
    if wire_format:
        result.setdefault('reasoning', {})['wire_format'] = wire_format
    if row.get('responses_profile'):
        result['responses'] = {'feature_profile': row['responses_profile']}
    explicit = row.get('inference_profile')
    if explicit is not None:
        explicit = validate_inference_profile(explicit)
        for key, value in explicit.items():
            if key in {'reasoning', 'sampling', 'responses'}:
                section = result.setdefault(key, {})
                if key == 'reasoning' and value.get('support') in {'unsupported', 'unknown'}:
                    section.clear()
                section.update(value)
            else:
                result[key] = value
    if result:
        result.setdefault('source', source)
    return validate_inference_profile(result)


def resolve_inference_profile(*, model=None, offering=None, connection=None, deployment=None) -> dict:
    """Most-specific declared subfields win; explicit unsupported clears enables."""
    result = {}
    for entity in (model, offering, connection, deployment):
        if not isinstance(entity, Mapping):
            continue
        profile = copy.deepcopy(entity.get('inference_profile') or {})
        for section, value in profile.items():
            if section in {'reasoning', 'sampling', 'responses'}:
                previous = result.setdefault(section, {})
                if section == 'reasoning' and value.get('support') in {'unsupported', 'unknown'}:
                    previous.clear()
                if section == 'reasoning' and 'supported_efforts' in value:
                    # A provider may narrow the ladder without repeating an
                    # inherited default that no longer exists on that route.
                    for key in ('default_effort', 'disabled_effort'):
                        if key not in value and previous.get(key) not in value['supported_efforts']:
                            previous.pop(key, None)
                    if 'effort_map' not in value and 'effort_map' in previous:
                        previous['effort_map'] = {alias: target for alias, target in previous['effort_map'].items()
                                                  if target in value['supported_efforts']}
                    # Narrowing can also drop the rung that made an inherited
                    # can_disable representable on an effort-ladder wire: with
                    # neither 'none' nor an in-ladder disabled_effort, "off"
                    # cannot compile, so the claim must narrow with the ladder
                    # instead of producing an uncompilable merged profile.
                    if ('can_disable' not in value and previous.get('can_disable') is True
                            and (value.get('wire_format') or previous.get('wire_format')) == 'reasoning_effort'
                            and 'none' not in value['supported_efforts']
                            and previous.get('disabled_effort') not in value['supported_efforts']):
                        previous['can_disable'] = False
                previous.update(value)
            else:
                result[section] = value
    reasoning = result.get('reasoning', {})
    if ((connection or {}).get('protocol') in {'openai_responses', 'responses'}
            and reasoning.get('support') == 'supported'
            and reasoning.get('supported_efforts')
            and 'wire_format' not in reasoning):
        # Responses defines the effort wire, while the catalog supplies the
        # model-specific ladder. This also binds saved pre-profile catalogs;
        # it grants no support and never overrides an explicit dialect/none.
        reasoning['wire_format'] = 'reasoning_effort'
    if reasoning.get('support') == 'unsupported':
        result['reasoning'] = {'support': 'unsupported', 'wire_format': 'none'}
        result.get('sampling', {}).pop('enabled_temperature', None)
    return validate_inference_profile(result)


def candidate_inference_metadata(candidate) -> dict:
    """Identical protocol/profile propagation for pooled and request-scoped slots."""
    entities = {name: getattr(candidate, name) for name in ('model', 'offering', 'connection', 'deployment')}
    # Binding is the last compatibility boundary for previously stored aliases.
    bound = {name: dict(row or {}, inference_profile=profile_from_metadata(row))
             for name, row in entities.items()}
    profile = resolve_inference_profile(**bound)
    # An installed v2 route always uses declared compilation. Missing catalog
    # evidence must not reactivate the legacy wire-model-name heuristics.
    profile.setdefault('reasoning', {}).setdefault('support', 'unknown')
    # An empty v2 profile (no catalog evidence anywhere) carries no window —
    # it must still resolve instead of raising on the absent key.
    windows = []
    offering_window = (candidate.offering or {}).get('context_window')
    if offering_window is not None:
        windows.append(int(offering_window))
    fallback_source = candidate.model or candidate.offering or {}
    fallback_window = fallback_source.get('context_window')
    if fallback_window is not None:
        windows.append(int(fallback_window))
    return {
        'context_window': min(windows) if windows else None,
        'inference_profile': profile,
        'thinking_format': profile.get('reasoning', {}).get('wire_format', '') or str(candidate.connection.get('thinking_format') or ''),
        'responses_profile': profile.get('responses', {}).get('feature_profile', ''),
    }
